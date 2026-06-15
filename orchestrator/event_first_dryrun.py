# -*- coding: utf-8 -*-
"""orchestrator/event_first_dryrun.py — СУХОЙ ПРОГОН event-first конвейера РЯДОМ с боевым.

Сшивает уже построенные и протестированные куски БЕЗ перепрошивки run_funnel и БЕЗ LLM/seal:
  Этап1 event_scan (открыто) → шок-источники → Этап2 cascade (амплитуда из ист.чувствительности)
  → Этап4 cascade_resolve (§9-прогноз ИЛИ лист ожидания). Демонстрация перед сшиванием контура.

Ничего не запечатывает (только готовит §9-спеки), пишет протокол как mode='mock' (бот его не пушит
как идею — гейтинг mock). Шок узла = последняя дневная лог-доходность источника (реальный ход).
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


def dry_run(horizon_days=5, max_sources=4, write=True):
    universe = yaml.safe_load(open(ROOT / "config" / "universe.yaml", encoding="utf-8")) or {}
    links = (yaml.safe_load(open(ROOT / "knowledge" / "causal_links.yaml", encoding="utf-8")) or {}).get("links")
    con = sqlite3.connect(str(DB))
    now = _now()
    run_id = "eventfirst_" + now.strftime("%Y%m%dT%H%M%SZ")
    try:
        scan = ES.scan_events_live(q_max=0.1, con=con)

        # шок-источники: (а) символы значимых ценовых сигналов; (б) прокси тем, чьи слова совпали
        # с салиентными новостными кластерами (событие дня → инструмент-исток).
        sources = []
        for s in scan["сигналы"]:
            if s.get("сигнал_после_FDR") and s.get("символ"):
                sources.append(s["символ"])
        for ne in scan["новостные_события"][:8]:
            theme, overlap = EM.match_cluster_to_theme(
                {"keywords": ne["ключи"], "sample": ne["пример"]}, universe)
            if theme:
                proxy = ((universe.get("themes") or {}).get(theme) or {}).get("proxy_etf")
                if proxy:
                    sources.append(proxy)
        # уникальные, только §9-разрешимые как ИСТОЧНИК, ограничим
        seen, uniq = set(), []
        for s in sources:
            if s in seen or not U.is_sealable(s, con=con):
                continue
            seen.add(s)
            uniq.append(s)
        uniq = uniq[:max_sources]

        cascades = []
        for src in uniq:
            shock = _last_return(con, src)
            if shock is None:
                continue
            nbrs = _neighbors(src, links)
            if not nbrs:
                continue
            casc = CAS.cascade_from_quotes(src, shock, nbrs, horizon_days=horizon_days, links=links)
            res = CR.resolve_cascade(casc, run_id=run_id, now_dt=now, con=con)
            cascades.append({"источник": src, "shock": round(shock, 5),
                             "резолв": res})
    finally:
        con.close()

    seal_total = sum(len(c["резолв"]["запечатываемо"]) for c in cascades)
    watch_total = sum(len(c["резолв"]["лист_ожидания"]) for c in cascades)
    protocol = {
        "run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": "mock",
        "режим": "event-first СУХОЙ ПРОГОН (рядом с боевым, без LLM/seal/run_funnel)",
        "spec_ref": "§6 Эт.1 скан + §5/П5 каскад + §9 резолв; PLAN_cascade_first Этапы 1/2/4",
        "скан": {"источники": scan["источники"], "сырых_сигналов": scan["сырых_сигналов"],
                 "статистических_после_FDR": scan["статистических_после_FDR"],
                 "топ_события": [e["метка"] for e in scan["кандидат_события"][:10]],
                 "ограничение_П8": scan["ограничение_П8"]},
        "шок_источники": uniq,
        "каскады": cascades,
        "итог": f"источников {len(uniq)}; §9-прогнозов-кандидатов {seal_total}; в лист ожидания {watch_total}",
    }
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / f"{run_id}.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    return protocol


if __name__ == "__main__":
    p = dry_run()
    s = p["скан"]
    print(f"[{p['run_id']}] {p['режим']}")
    print(f"  СКАН: {s['источники']} = {s['сырых_сигналов']} сигналов, "
          f"после FDR {s['статистических_после_FDR']}")
    print(f"  топ-события: {', '.join(s['топ_события'][:6])}")
    print(f"  шок-источники: {p['шок_источники']}")
    for c in p["каскады"]:
        r = c["резолв"]
        print(f"\n  ⚡ {c['источник']} шок={c['shock']:+.4f} → узлы:")
        for sp in r["запечатываемо"]:
            pr = sp["prediction"]
            print(f"     ✅ SEAL  {pr['asset']:8} {pr['direction']:5} thr={pr['threshold']} "
                  f"P={pr['probability']} amp={pr['amplitude_expected']}")
        for w in r["лист_ожидания"]:
            print(f"     ⏳ WAIT  {w['актив']:8} — {w['причина'][:55]}")
    print(f"\n  ИТОГ: {p['итог']}")
