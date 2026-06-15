# -*- coding: utf-8 -*-
"""orchestrator/event_first.py — EVENT-FIRST КОНТУР end-to-end (Этап 6 PLAN_cascade_first.md).

Сшивает всё: открытый скан §6 (event_scan) → шок-источники из событий → ДЛЯ КАЖДОГО прогоняется
ПОЛНЫЙ состязательный контур run_funnel (агенты §4 + слепой суд П10, mock/live) ДЛЯ качественной
проверки (тайминг/манипуляция/кто-продаёт-нам/дебаты) + ДЕТЕРМИНИРОВАННЫЙ каскад-резолв §9
(амплитуда из калиброванной чувствительности) для торгуемых спеков. Дирижёр сводит и диверсифицирует.

Развязка открытие/запечатывание: контур и каскад открыты по событиям; запечатываемые §9-спеки —
только разрешимые (cascade_resolve), остальное — лист ожидания. seal в журнал — отдельно (live + §11).
mode='mock' прогоняет агентов БЕЗ сети/трат — доказательство сшивки перед live.
"""
import json
import sqlite3
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from orchestrator import event_scan as ES            # noqa: E402
from orchestrator import cascade_resolve as CR       # noqa: E402
from orchestrator import universe_resolver as U      # noqa: E402
from orchestrator import event_mapping as EM         # noqa: E402
from orchestrator import funnel as F                  # noqa: E402
from orchestrator import multi_event as ME            # noqa: E402
from mathlib import cascade as CAS                    # noqa: E402

DB = ROOT / "storage" / "oracle.db"
LOGS = ROOT / "journal" / "funnel_logs"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _last_return(con, symbol):
    rows = con.execute(
        "SELECT adjusted_close FROM quotes WHERE symbol=? AND adjusted_close IS NOT NULL "
        "ORDER BY date DESC LIMIT 6", (symbol,)).fetchall()
    px = [float(r[0]) for r in rows][::-1]
    r = CAS.log_returns(px)
    return float(r[-1]) if r.size else None


def _neighbors(symbol, links):
    out = []
    for ln in (links or []):
        pr = [str(x) for x in (ln.get("pair") or [])]
        if symbol in pr and len(pr) == 2:
            out.append(pr[1] if pr[0] == symbol else pr[0])
    return sorted(set(out))


def _shock_sources(scan, universe, con, max_sources):
    """Источники шока из ОТКРЫТОГО скана: значимые ценовые сигналы + прокси тем, чьи слова совпали
    с салиентными новостными кластерами (событие дня → инструмент-исток)."""
    cand = []
    for s in scan["сигналы"]:
        if s.get("сигнал_после_FDR") and s.get("символ"):
            cand.append(s["символ"])
    for ne in scan["новостные_события"][:8]:
        theme, _ = EM.match_cluster_to_theme({"keywords": ne["ключи"], "sample": ne["пример"]}, universe)
        if theme:
            proxy = ((universe.get("themes") or {}).get(theme) or {}).get("proxy_etf")
            if proxy:
                cand.append(proxy)
    seen, uniq = set(), []
    for s in cand:
        if s in seen or not U.is_sealable(s, con=con):
            continue
        seen.add(s)
        uniq.append(s)
    return uniq[:max_sources]


def run_event_first(mode="mock", k=3, horizon_days=5, write=True, run_id=None):
    """Полный event-first прогон. Возвращает сводный протокол."""
    run_id = run_id or "ef_" + _now().strftime("%Y%m%dT%H%M%SZ")
    universe = yaml.safe_load(open(ROOT / "config" / "universe.yaml", encoding="utf-8")) or {}
    links = (yaml.safe_load(open(ROOT / "knowledge" / "causal_links.yaml", encoding="utf-8")) or {}).get("links")
    now = _now()
    con = sqlite3.connect(str(DB))
    try:
        scan = ES.scan_events_live(q_max=0.1, con=con)
        sources = _shock_sources(scan, universe, con, k)
        per_source, all_ideas = [], []
        for src in sources:
            # 1) КАЧЕСТВЕННО: полный состязательный контур, заякоренный на источник события
            p = F.run_funnel(theme=src, mode=mode, theme_focused=True, write=write,
                             run_id=f"{run_id}__{src}")
            ideas = (p.get("этап6_синтез") or {}).get("отчёты", [])
            for idea in ideas:
                all_ideas.append({**idea, "_событие": src})
            fr = p.get("воронка_отсева") or {}
            # 2) КОЛИЧЕСТВЕННО: каскад из калиброванной чувствительности → §9-резолв
            shock = _last_return(con, src)
            nbrs = _neighbors(src, links)
            casc_res = None
            if shock is not None and nbrs:
                casc = CAS.cascade_from_quotes(src, shock, nbrs, horizon_days=horizon_days, links=links)
                casc_res = CR.resolve_cascade(casc, run_id=run_id, now_dt=now, con=con)
            per_source.append({
                "источник": src, "shock": (round(shock, 5) if shock is not None else None),
                "контур": {"run_id": p.get("run_id"), "кандидатов": p.get("candidates_count"),
                           "выдано": fr.get("этап6_выдано_топ", 0),
                           "итог": fr.get("вывод") or "—"},
                "каскад_резолв": ({"запечатываемо": casc_res["запечатываемо"],
                                   "лист_ожидания": casc_res["лист_ожидания"],
                                   "сводка": casc_res["сводка"]} if casc_res else None),
            })
    finally:
        con.close()

    top3 = ME._diversify(all_ideas)
    seal_total = sum(len((s.get("каскад_резолв") or {}).get("запечатываемо", [])) for s in per_source)
    watch_total = sum(len((s.get("каскад_резолв") or {}).get("лист_ожидания", [])) for s in per_source)
    protocol = {
        "run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
        "режим": "event-first контур (§6 скан + §4 контур + §5 каскад + §9 резолв)",
        "spec_ref": "PLAN_cascade_first Этап 6; §6/§4/§5/П5/§9/П10/П16",
        "скан": {"источники": scan["источники"], "сырых_сигналов": scan["сырых_сигналов"],
                 "статистических_после_FDR": scan["статистических_после_FDR"],
                 "топ_события": [e["метка"] for e in scan["кандидат_события"][:10]]},
        "шок_источники": sources,
        "по_источникам": per_source,
        "контур_выдал_топ3": [{"актив": i.get("актив"), "направление": i.get("направление"),
                               "балл": i.get("балл"), "событие": i.get("_событие")} for i in top3],
        "итог": (f"источников {len(sources)}; контур выдал {len(top3)} идей; "
                 f"каскад: §9-спеков {seal_total}, в лист ожидания {watch_total}"),
    }
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{run_id}.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    return protocol


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="mock", choices=["mock", "live", "auto"])
    ap.add_argument("--k", type=int, default=3)
    a = ap.parse_args()
    p = run_event_first(mode=a.mode, k=a.k)
    print(f"[{p['run_id']}] {p['режим']} · mode={p['mode']}")
    print(f"  скан: {p['скан']['сырых_сигналов']} сигналов, события: {', '.join(p['скан']['топ_события'][:5])}")
    print(f"  {p['итог']}")
