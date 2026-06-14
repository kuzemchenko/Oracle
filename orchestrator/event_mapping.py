# -*- coding: utf-8 -*-
"""orchestrator/event_mapping.py — авто-привязка обнаруженных НОВОСТНЫХ КЛАСТЕРОВ к торгуемым
инструментам (долг №3). Замыкает мульти-событийный режим: новое событие, которого ещё нет
среди зарегистрированных тем, превращается в ЧЕРНОВУЮ каскадную карту с проверяемыми тикерами.

ЧЕСТНАЯ ДИСЦИПЛИНА:
  1. Известный кластер → матчится по ключевым словам к зарегистрированной теме/цепочке
     (детерминированно) → уже покрыт якорным контуром мульти-режима.
  2. Новый кластер → LLM-мэппер предлагает каскад 2–4 порядка с КАНДИДАТ-тикерами (П5).
     Это ГИПОТЕЗА, не сделка.
  3. Тикеры проверяются ДЕТЕРМИНИРОВАННО (есть ли данные/ликвидность) — выдумки отсеиваются (П8).
  4. Прошедшие → СТЕЙДЖАТСЯ в journal/proposed_themes.jsonl на регистрацию человеком (§30).
     НИКОГДА не авто-торгуются: нет истории/калибровки → нет запечатанного прогноза (П16).
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROPOSED = ROOT / "journal" / "proposed_themes.jsonl"


def make_eodhd_checker(api_key):
    """Фабрика чекера тикера через EODHD: существует ли + средний объём (для verify_tickers)."""
    import datetime
    from data import eodhd as E

    def checker(ticker):
        today = datetime.date.today()
        rows = E.fetch_eod(ticker, api_key,
                           (today - datetime.timedelta(days=40)).isoformat(), today.isoformat())
        if not rows:
            return None
        vols = [r.get("volume") for r in rows if r.get("volume")]
        avg = sum(vols) / len(vols) if vols else 0
        return {"avg_volume": avg, "last": rows[-1].get("close")}
    return checker


def match_cluster_to_theme(cluster, universe):
    """Детерминированный матч кластера к зарегистрированной теме по пересечению ключевых слов.

    Возвращает (theme_name, overlap) при наличии совпадения, иначе (None, 0)."""
    from orchestrator import context as C
    themes = (universe or {}).get("themes") or {}
    ckw = {k.lower() for k in (cluster.get("keywords") or [])}
    sample = (cluster.get("sample") or "").lower()
    best, best_n = None, 0
    for name in themes:
        tkw = set(C._theme_keywords(name, universe))
        overlap = len(ckw & tkw) + sum(1 for k in tkw if k in sample)
        if overlap > best_n:
            best, best_n = name, overlap
    return (best, best_n) if best_n > 0 else (None, 0)


MAPPER_SYSTEM = (
    "Ты — картограф каскадов «Оракула». По НОВОСТНОМУ КЛАСТЕРУ (ключевые слова + заголовок) "
    "построй торгуемую каскадную карту 2–4 порядка (П5: 1-й порядок отыгран HFT — ищи дальние "
    "звенья-чокпоинты). НЕ выдумывай: если событие неясно или нет торгуемого переноса — верни "
    "пустой каскад и честно скажи почему. Тикеры — ликвидные US-инструменты (ETF/крупные акции), "
    "формат СИМВОЛ.US; это КАНДИДАТЫ на проверку, не сделка. Верни РОВНО один объект JSON:\n"
    '{"событие":"...","первый_порядок":"...","каскад":[{"порядок":2,"узел":"...",'
    '"тикеры":["XXX.US"],"чокпоинт":true}],"обоснование":"...","уверенность":"низкая|средняя|высокая"}'
)


def propose_cascade(cluster, client, run_id="event_map"):
    """LLM-мэппер: кластер → черновая каскадная карта. client — openrouter-клиент (внедряем в тестах)."""
    user = ("Новостной кластер дня:\n"
            f"- ключевые слова: {cluster.get('keywords')}\n"
            f"- заголовок: {cluster.get('sample')}\n"
            f"- салиентность (число статей): {cluster.get('salience')}\n\n"
            "Построй торгуемую каскадную карту по контракту из системного промпта.")
    res = client.complete("generator", MAPPER_SYSTEM, user,
                          agent_id="event_mapper", output_kind="generator_hypothesis")
    txt = (res.get("text") or "").strip()
    try:
        start, end = txt.find("{"), txt.rfind("}")
        return json.loads(txt[start:end + 1]) if start >= 0 else None
    except (json.JSONDecodeError, ValueError):
        return None


def verify_tickers(tickers, checker):
    """Детерминированная проверка кандидат-тикеров: оставить только реальные/ликвидные.

    checker(ticker) -> dict|None: {avg_volume, last} если инструмент существует, иначе None.
    Выдуманные/неликвидные тикеры отсеиваются (П8 + фильтр ликвидности §14)."""
    MIN_VOL = 100000
    out = []
    for t in tickers or []:
        try:
            info = checker(t)
        except Exception:  # noqa: BLE001
            info = None
        if info and (info.get("avg_volume") or 0) >= MIN_VOL:
            out.append({"ticker": t, "avg_volume": info.get("avg_volume"), "last": info.get("last")})
    return out


def map_cluster(cluster, universe, client, checker):
    """Полный цикл по одному кластеру: матч к теме ИЛИ предложение+верификация черновой карты."""
    theme, overlap = match_cluster_to_theme(cluster, universe)
    if theme:
        return {"kind": "matched", "theme": theme, "overlap": overlap, "cluster": cluster}
    draft = propose_cascade(cluster, client)
    if not draft or not draft.get("каскад"):
        return {"kind": "no_map", "cluster": cluster,
                "why": (draft or {}).get("обоснование", "торгуемого переноса не найдено")}
    verified_nodes = []
    for node in draft.get("каскад", []):
        vt = verify_tickers(node.get("тикеры"), checker)
        if vt:
            verified_nodes.append({**node, "verified_tickers": vt})
    return {"kind": "proposed", "cluster": cluster, "draft": draft,
            "verified_nodes": verified_nodes,
            "tradable": bool(verified_nodes)}


def stage_proposal(mapped, ts, path=PROPOSED):
    """Записать предложенную тему в стейджинг (append-only) на регистрацию человеком (§30)."""
    rec = {"ts": ts, "событие": mapped["draft"].get("событие"),
           "уверенность": mapped["draft"].get("уверенность"),
           "узлы": [{"порядок": n.get("порядок"), "узел": n.get("узел"),
                     "тикеры": [v["ticker"] for v in n["verified_tickers"]],
                     "чокпоинт": n.get("чокпоинт")} for n in mapped["verified_nodes"]],
           "обоснование": mapped["draft"].get("обоснование"),
           "кластер_ключи": mapped["cluster"].get("keywords"),
           "статус": "предложено (на регистрацию, НЕ торгуется до утверждения §30/П16)"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
