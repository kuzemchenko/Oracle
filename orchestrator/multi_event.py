# -*- coding: utf-8 -*-
"""orchestrator/multi_event.py — МУЛЬТИ-СОБЫТИЙНЫЙ режим (§17.1 апгрейд).

Лечит «всё схлопывается в одну громкую тему»: вместо одного прогона по топ-новости дня —
  1) скан дробит день на РАЗЛИЧИМЫЕ события (зарегистрированные темы + кластеры новостей);
  2) каждое ранжируется по ТЕКТОНИЧЕСКОМУ потенциалу (mathlib.tectonic) / салиентности;
  3) по топ-K ЯКОРИМЫМ темам гоняется заякоренный контур (funnel theme_focused — без дрейфа);
  4) Дирижёр сводит выдачи и ДИВЕРСИФИЦИРУЕТ по макро-драйверам (§4 портфель).

Громкое-но-отыгранное (1-й порядок) получает низкий приоритет (П5/П13). Обнаруженные, но ещё
НЕ якоримые кластеры новостей — surfaced для регистрации человеком (не авто-прогон: без карты
каскада они увели бы в дрейф). Так ни одно тектоническое событие не монополизирует и не теряется.
"""
import re
import json
import pathlib

from orchestrator import context as C
from orchestrator import funnel as F
from mathlib import tectonic as TEC

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOGS = ROOT / "journal" / "funnel_logs"


def _write(protocol):
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{protocol['run_id']}.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Мульти-событийный прогон {protocol['run_id']}", "",
             f"_{protocol['режим']} · {protocol['spec_ref']}_", "",
             "## Ранжирование событий по тектоническому потенциалу", ""]
    for e in protocol["ранжирование_событий"]:
        t = e.get("tectonic")
        far = (t or {}).get("далёкий_узел") or {}
        lines.append(f"- **{e['id']}** · приоритет {e['score']}"
                     + (f" · T={t['T']} → дальний узел {far.get('instruments')}" if t else "")
                     + ("" if e["anchorable"] else " · _не якоримо_"))
    lines += ["", "## Глубоко проанализировано (заякоренные контуры)", ""]
    for pe in protocol["по_событиям"]:
        lines.append(f"- **{pe['событие']}**: кандидатов {pe['кандидатов']}, выдано "
                     f"{pe['выдано']} — {pe['итог']}")
    lines += ["", "## Объединённая выдача (диверсифицировано по драйверам §4)", ""]
    if protocol["объединённая_выдача_топ3"]:
        for i in protocol["объединённая_выдача_топ3"]:
            lines.append(f"- {i['актив']} {i['направление']} (балл {i['балл']}, событие {i['событие']})")
    else:
        lines.append("- стоящих идей нет — легитимный результат (§6)")
    lines += ["", "## Обнаруженные кластеры новостей (для регистрации как тем)", ""]
    for cl in protocol["обнаруженные_кластеры_новостей"][:6]:
        lines.append(f"- салиентность {cl['salience']} · {cl['keywords']} · «{(cl['sample'] or '')[:70]}»")
    lines += ["", "## Привязка кластеров (долг №3: матч к темам / черновые карты)", ""]
    for m in protocol.get("привязка_кластеров", []):
        if m["kind"] == "matched":
            lines.append(f"- {m['keywords']} → тема **{m['theme']}**" + (" (проанализирована)" if m.get("covered") else ""))
        elif m["kind"] == "proposed":
            nodes = "; ".join(f"{n['узел']}={n['тикеры']}" for n in m["узлы"])
            t = m.get("тектонический_потенциал")
            far = (m.get("целевой_дальний_узел") or {}).get("instruments")
            lines.append(f"- {m['keywords']} → ПРЕДЛОЖЕНО «{m['событие']}» (T={t}, цель {far}): "
                         f"{nodes} _(застейджено на регистрацию §30)_")
        else:
            lines.append(f"- {m['keywords']} → {m['kind']}: {m.get('why','')}")
    lines += ["", f"**Итог:** {protocol['итог']}", ""]
    (LOGS / f"{protocol['run_id']}.md").write_text("\n".join(lines), encoding="utf-8")

_STOP = set("the a an of to in on for and or is are be with from at by as this that "
            "и в на по с от за до о об у к не что как для при из его их же бы то "
            "的 了 是 在 和 与".split())


def _tokens(title):
    return {t for t in re.findall(r"[^\W\d_]{3,}", (title or "").lower()) if t not in _STOP}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def detect_news_clusters(news, threshold=0.30, top=6):
    """Детерминированная кластеризация заголовков (жадно по Жаккару токенов). Возвращает
    список кластеров {size, salience, keywords, sample} по убыванию салиентности."""
    clusters = []  # каждый: {"rep": set, "items": [titles], "tokfreq": {}}
    for it in news:
        toks = _tokens(it.get("title"))
        if not toks:
            continue
        best, bestj = None, 0.0
        for cl in clusters:
            j = _jaccard(toks, cl["rep"])
            if j > bestj:
                best, bestj = cl, j
        if best and bestj >= threshold:
            best["items"].append(it.get("title"))
            best["rep"] |= toks
            for t in toks:
                best["tokfreq"][t] = best["tokfreq"].get(t, 0) + 1
        else:
            clusters.append({"rep": set(toks), "items": [it.get("title")],
                             "tokfreq": {t: 1 for t in toks}})
    out = []
    for cl in clusters:
        kws = sorted(cl["tokfreq"], key=cl["tokfreq"].get, reverse=True)[:4]
        out.append({"size": len(cl["items"]), "salience": len(cl["items"]),
                    "keywords": kws, "sample": cl["items"][0]})
    out.sort(key=lambda c: c["salience"], reverse=True)
    return out[:top]


def recent_news(limit=300):
    """Широкий срез свежих новостей для кластеризации (дефолтный ctx-срез — лишь 12, мало)."""
    import sqlite3
    if not C.DB.exists():
        return []
    con = sqlite3.connect(C.DB)
    try:
        return C._news(con, limit=limit)
    finally:
        con.close()


def rank_events(universe=None, news=None):
    """Единый ранг кандидат-событий: зарегистрированные темы (по тектонике/типу) + кластеры новостей.

    Якоримые темы (есть proxy в универсуме) — кандидаты на авто-прогон; кластеры без карты —
    только surfaced. Связь с новостями дня: тема, чьи ключевые слова совпали с салиентным
    кластером, получает прибавку (актуальна сегодня)."""
    universe = universe or C._load_yaml("config/universe.yaml")
    themes = universe.get("themes") or {}
    clusters = detect_news_clusters(news or [])
    cluster_kw = {k for cl in clusters for k in cl["keywords"]}

    events = []
    for name, meta in themes.items():
        chain_id = meta.get("cascade_chain")
        structural = bool(meta.get("structural"))
        tect = None
        if chain_id:
            ch = TEC.get_chain(chain_id)
            tect = TEC.score_chain(ch) if ch else None
            base = tect["entry_score"] if tect else 0.5
        elif structural:
            base = 0.6                      # событие-драйвер, якоримо (research)
        else:
            base = 0.4                      # калибровочная тема без карты
        # актуальность по новостям дня
        kws = set(C._theme_keywords(name, universe))
        news_match = 0.3 if (kws & cluster_kw) else 0.0
        events.append({
            "kind": "theme", "id": name, "anchorable": bool(meta.get("proxy_etf")),
            "structural": structural, "score": round(min(base + news_match, 1.0), 4),
            "tectonic": (tect and {"T": tect["tectonic_potential"], "далёкий_узел": tect["best_far_node"],
                                   "entry": tect["entry_score"]}),
            "событие": meta.get("event"),
        })
    events.sort(key=lambda e: e["score"], reverse=True)
    return {"события": events, "кластеры_новостей": clusters}


def _diversify(ideas):
    """Лучшая идея на каждый макро-драйвер → топ-3 (карта корреляций §4: одно событие = одна ставка)."""
    by_driver = {}
    for idea in ideas:
        drv = (idea.get("позиция") or {}).get("макро_драйвер") or idea.get("_событие") or idea.get("актив")
        cur = by_driver.get(drv)
        if cur is None or (idea.get("балл") or 0) > (cur.get("балл") or 0):
            by_driver[drv] = idea
    top = sorted(by_driver.values(), key=lambda i: i.get("балл") or 0, reverse=True)
    return top[:3]


def _map_new_clusters(clusters, deep_themes, universe, mode, run_id, max_map=2):
    """Долг №3: обнаруженные кластеры → матч к теме ИЛИ черновая каскадная карта (LLM+верификация).

    Матченные к уже глубоко-проанализированным темам пропускаем (покрыты). Новые (top max_map по
    салиентности) → propose+verify+stage на регистрацию (НЕ авто-торгуются, П16/§30)."""
    from orchestrator import openrouter as OR
    from orchestrator import event_mapping as EM
    import os
    out = []
    client = None
    checker = None
    mapped_count = 0
    for cl in clusters:
        theme, overlap = EM.match_cluster_to_theme(cl, universe)
        if theme:
            out.append({"kind": "matched", "theme": theme, "keywords": cl["keywords"],
                        "covered": theme in deep_themes})
            continue
        if mapped_count >= max_map:
            out.append({"kind": "skipped", "keywords": cl["keywords"], "why": "лимит маппинга на прогон"})
            continue
        if client is None:
            client = OR.make_client(mode=mode, run_id=run_id)
            checker = EM.make_eodhd_checker(os.environ.get("EODHD_API_KEY", ""))
        mapped_count += 1
        m = EM.map_cluster(cl, universe, client, checker)
        if m["kind"] == "proposed" and m["tradable"]:
            rec = EM.stage_proposal(m, F._now_iso())
            tec = m.get("tectonic") or {}
            out.append({"kind": "proposed", "keywords": cl["keywords"],
                        "событие": rec["событие"], "узлы": rec["узлы"], "staged": True,
                        "тектонический_потенциал": tec.get("tectonic_potential"),
                        "целевой_дальний_узел": tec.get("best_far_node")})
        else:
            out.append({"kind": m["kind"], "keywords": cl["keywords"],
                        "why": m.get("why", "торгуемого переноса не найдено")})
    # предложенные — по убыванию тектонического потенциала (долг №4: ранг новых событий по T)
    out.sort(key=lambda x: (x.get("тектонический_потенциал") or -1), reverse=True)
    return out


def run_multi_event(mode="auto", k=3, write=True, run_id=None, map_clusters=True):
    """Полный мульти-событийный прогон. Возвращает сводный протокол."""
    run_id = run_id or f"multi_{F._now_compact()}"
    universe = C._load_yaml("config/universe.yaml")
    ranking = rank_events(universe=universe, news=recent_news(300))  # широкий срез для кластеров
    anchorable = [e for e in ranking["события"] if e["anchorable"]][:k]

    per_event, all_ideas = [], []
    for ev in anchorable:
        p = F.run_funnel(theme=ev["id"], mode=mode, theme_focused=True,
                         write=write, run_id=f"{run_id}__{ev['id']}")
        fr = p.get("воронка_отсева") or {}
        ideas = (p.get("этап6_синтез") or {}).get("отчёты", [])
        for idea in ideas:
            idea = {**idea, "_событие": ev["id"]}
            all_ideas.append(idea)
        per_event.append({
            "событие": ev["id"], "приоритет": ev["score"], "тектоника": ev.get("tectonic"),
            "run_id": p.get("run_id"), "кандидатов": p.get("candidates_count"),
            "выдано": fr.get("этап6_выдано_топ", 0),
            "итог": fr.get("вывод") or p.get("ОТКАЗ_тема", {}).get("reason") or "—",
        })

    # Долг №3: обнаруженные кластеры → матч к темам ИЛИ черновые каскадные карты (на регистрацию).
    deep = {e["id"] for e in anchorable}
    mapped = (_map_new_clusters(ranking["кластеры_новостей"], deep, universe, mode, run_id)
              if map_clusters else [])

    top3 = _diversify(all_ideas)
    protocol = {
        "run_id": run_id, "ts": F._now_iso(), "mode": mode, "режим": "мульти-событие (§17.1)",
        "spec_ref": "§17.1 свободная генерация + §5/П5 тектоника + §4 диверсификация по драйверам",
        "ранжирование_событий": ranking["события"],
        "обнаруженные_кластеры_новостей": ranking["кластеры_новостей"],
        "привязка_кластеров": mapped,   # №3: матч к темам / черновые карты на регистрацию
        "глубоко_проанализировано": [e["id"] for e in anchorable],
        "по_событиям": per_event,
        "объединённая_выдача_топ3": [
            {"актив": i.get("актив"), "направление": i.get("направление"),
             "балл": i.get("балл"), "событие": i.get("_событие")} for i in top3],
        "диверсификация": "лучшая идея на макро-драйвер (§4); громкий 1-й порядок не монополизирует (П5/П13)",
        "итог": (f"проанализировано {len(anchorable)} тектонических тем; "
                 f"выдано {len(top3)} диверсифицированных идей"
                 if top3 else
                 f"проанализировано {len(anchorable)} тем; стоящих идей нет (§6) — легитимно"),
    }
    if write:
        _write(protocol)
    return protocol
