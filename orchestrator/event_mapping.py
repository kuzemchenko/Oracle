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


def make_eodhd_type_lookup(api_key):
    """Фабрика лукапа ТИПА инструмента (General.Type: 'Common Stock'/'ETF'/…) через EODHD-фундаментал.
    Кэш на тикер, best-effort (нет данных → None). Нужен, чтобы предпочесть КОМПАНИЮ секторному ETF."""
    from data import eodhd as E
    cache = {}

    def type_of(ticker):
        if ticker in cache:
            return cache[ticker]
        typ = None
        try:
            fnd = E.fetch_fundamentals(ticker, api_key) or {}
            typ = (fnd.get("General") or {}).get("Type")
        except Exception:  # noqa: BLE001
            typ = None
        cache[ticker] = typ
        return typ
    return type_of


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


def verify_tickers(tickers, checker, type_lookup=None):
    """Детерминированная проверка кандидат-тикеров: оставить только реальные/ликвидные.

    checker(ticker) -> dict|None: {avg_volume, last} если инструмент существует, иначе None.
    type_lookup(ticker) -> str|None: тип (Common Stock/ETF…) — запрашивается ТОЛЬКО для прошедших
    фильтр объёма (дёшево), чтобы потом предпочесть компанию ETF-у. Выдумки/неликвид отсеиваются (П8/§14)."""
    MIN_VOL = 100000
    out = []
    for t in tickers or []:
        try:
            info = checker(t)
        except Exception:  # noqa: BLE001
            info = None
        if info and (info.get("avg_volume") or 0) >= MIN_VOL:
            typ = type_lookup(t) if type_lookup else None
            out.append({"ticker": t, "avg_volume": info.get("avg_volume"),
                        "last": info.get("last"), "type": typ})
    return out


def _proposal_to_chain(mapped):
    """Адаптер: верифицированная черновая карта → схема цепочки для mathlib.tectonic (долг №4).

    Лаги рёбер неизвестны (LLM их не даёт) → edges с lag_days=None: ось L честно = 0 (окно входа
    не откалибровано). priced_hint берём из глубины/чокпоинта (дальний чокпоинт обычно low).
    M/P/S не заданы → tectonic поставит нейтраль 0.5 с пометкой (П8)."""
    nodes = []
    for n in mapped["verified_nodes"]:
        order = n.get("порядок") or 1
        choke = bool(n.get("чокпоинт"))
        priced = "low" if (choke and order >= 3) else ("medium" if order >= 2 else "high")
        # предпочесть КОНКРЕТНУЮ КОМПАНИЮ секторному ETF: Common Stock раньше ETF (§3c, не у истока)
        vts = sorted(n["verified_tickers"],
                     key=lambda v: 0 if (v.get("type") and v["type"] != "ETF") else 1)
        nodes.append({"order": order, "node": n.get("узел"),
                      "instruments": [v["ticker"] for v in vts],
                      "chokepoint": choke, "priced_hint": priced})
    orders = sorted({nd["order"] for nd in nodes})
    edges = [{"from": orders[i], "to": orders[i + 1], "lag_days": None, "strength": "unknown"}
             for i in range(len(orders) - 1)]
    return {"id": f"proposed:{(mapped['draft'].get('событие') or '')[:40]}",
            "title": mapped["draft"].get("событие"),
            "trigger": {},   # M/P/S неизвестны → нейтраль с пометкой
            "nodes": nodes, "edges": edges}


def score_proposal(mapped):
    """Тектонический балл предложенной карты (долг №4). None, если нет торгуемых узлов."""
    if mapped.get("kind") != "proposed" or not mapped.get("verified_nodes"):
        return None
    from mathlib import tectonic as TEC
    return TEC.score_chain(_proposal_to_chain(mapped))


def map_cluster(cluster, universe, client, checker, type_lookup=None):
    """Полный цикл по одному кластеру: матч к теме ИЛИ предложение+верификация черновой карты."""
    theme, overlap = match_cluster_to_theme(cluster, universe)
    if theme:
        return {"kind": "matched", "theme": theme, "overlap": overlap, "cluster": cluster}
    # Ревью 2026-07-04 HIGH: отказ LLM (все фолбеки роли исчерпаны → RuntimeError) раньше РОНЯЛ
    # весь дневной прогон: без протокола, бот молчал, гибла и детерминированная часть (авторские
    # каскады/граф/seal), которой LLM не нужен. Per-cluster fail-soft: сбой картографа → кластер
    # честно помечен и пропущен, контур жив. RunBudgetExceeded — BaseException, тут НЕ ловится
    # и легитимно останавливает прогон (§24).
    try:
        draft = propose_cascade(cluster, client)
    except Exception as e:  # noqa: BLE001
        return {"kind": "mapper_error", "cluster": cluster,
                "why": f"сбой LLM-картографа: {type(e).__name__}: {e}"}
    if not draft or not draft.get("каскад"):
        return {"kind": "no_map", "cluster": cluster,
                "why": (draft or {}).get("обоснование", "торгуемого переноса не найдено")}
    verified_nodes = []
    for node in draft.get("каскад", []):
        vt = verify_tickers(node.get("тикеры"), checker, type_lookup=type_lookup)
        if vt:
            verified_nodes.append({**node, "verified_tickers": vt})
    mapped = {"kind": "proposed", "cluster": cluster, "draft": draft,
              "verified_nodes": verified_nodes,
              "tradable": bool(verified_nodes)}
    mapped["tectonic"] = score_proposal(mapped)   # долг №4: балл T + дальний узел
    return mapped


def proposal_chains(clusters, universe, client, checker, type_lookup=None, max_map=8):
    """Картограф → ВОРОНКА (B2.5 §R2): новостные кластеры ВНЕ реестра тем → каскадные карты → схемы
    цепочек для graph_build. Так НОВОСТЬ становится каскадными узлами в общей воронке отбора, а не
    только «на регистрацию». Каждая верифицированная карта (LLM-гипотеза 2–4 порядка, ярус C) →
    _proposal_to_chain → (chain, anchor). Якорь = узел минимального порядка карты (его недавняя
    доходность даст шок свёртки вниз). max_map ограничивает LLM-вызовы (бюджет §30).

    Возвращает список {chain, anchor, событие, уверенность, ключи, mapped} (mapped — для стейджинга)."""
    out, mapped_n = [], 0
    for cl in (clusters or []):
        if mapped_n >= max_map:
            break
        theme, _ = match_cluster_to_theme(cl, universe)
        if theme:
            continue                         # покрыто авторской цепочкой
        mapped_n += 1
        m = map_cluster(cl, universe, client, checker, type_lookup=type_lookup)
        if m.get("kind") != "proposed" or not m.get("verified_nodes"):
            continue
        chain = _proposal_to_chain(m)
        nodes = sorted(chain["nodes"], key=lambda n: n.get("order", 0))
        anchor = (nodes[0].get("instruments") or [None])[0] if nodes else None
        if not anchor:
            continue
        out.append({"chain": chain, "anchor": anchor, "mapped": m,
                    "событие": m["draft"].get("событие"),
                    "уверенность": m["draft"].get("уверенность"), "ключи": cl.get("keywords")})
    return out


def stage_proposal(mapped, ts, path=PROPOSED):
    """Записать предложенную тему в стейджинг (append-only) на регистрацию человеком (§30)."""
    tec = mapped.get("tectonic") or {}
    rec = {"ts": ts, "событие": mapped["draft"].get("событие"),
           "уверенность": mapped["draft"].get("уверенность"),
           "тектонический_потенциал": tec.get("tectonic_potential"),
           "целевой_дальний_узел": tec.get("best_far_node"),
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
