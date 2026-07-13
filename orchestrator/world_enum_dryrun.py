# -*- coding: utf-8 -*-
"""orchestrator/world_enum_dryrun.py — MOCK END-TO-END обвязка каркаса Э4 «перебор мира».

Гейт Э4 (spec/ROADMAP_2026-07_search_engine.md): на ИСТОРИЧЕСКОМ событии ai_power (данные в БД,
карта известна независимо) прогнать конвейер каркаса:
  карта из ФИКСТУРЫ (orchestrator/fixtures/world_map_ai_power.json — live LLM в разработке
  ЗАПРЕЩЁН) → детерминированный скрин live-данными (EODHD screener при наличии ключа, иначе
  фолбэк Tier0-фундаментал БД) → перечисление инструментов (цель 100+) → retry-логика (д)
  с категориями отказов → протокол; бюджет события закэпован и виден.

Скоринг/ранг (в)(г) — заглушки «после Д3» (world_enum.score_pair_conditional/rank_pair).

БЕЗОПАСНОСТЬ КАРКАСА:
  • боевая БД открывается READ-ONLY (uri mode=ro) — добор истории здесь не выполняется
    (allow_history_fetch=False; в бою Э5 — без потолка, решение №5);
  • НИЧЕГО не запечатывается; боевые журналы predictions/outcomes не трогаются;
  • кандидат-рёбра (ж) по умолчанию НЕ пишутся в knowledge/edge_candidates.jsonl
    (--append-candidates включает: осмысленно только на реальном, не mock-прогоне);
  • протокол — в journal/world_enum_logs/ (свой каталог, mode=mock: бот такие не пушит).
"""
import argparse
import datetime
import json
import os
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml                                            # noqa: E402

from orchestrator import world_enum as WE              # noqa: E402
from mathlib import cascade as CAS                     # noqa: E402

DB = ROOT / "storage" / "oracle.db"
FIXTURE = ROOT / "orchestrator" / "fixtures" / "world_map_ai_power.json"


def _connect_ro(db=None):
    """READ-ONLY соединение с боевой БД: каркас Э4 не имеет права её менять."""
    p = pathlib.Path(db or DB)
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)


def _shock(con, symbol, asof):
    """Реальный шок источника за окно реакции §R2.1 (как edge_forward._shock: adjusted_close,
    asof-гейт). None → событие честно стопится «шок не подтверждён»."""
    rows = con.execute(
        "SELECT COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
        "AND COALESCE(adjusted_close, close) IS NOT NULL AND date <= ? "
        "ORDER BY date DESC LIMIT ?", (symbol, asof, CAS.EVENT_WINDOW_DAYS + 1)).fetchall()
    px = [float(r[0]) for r in rows][::-1]
    return CAS.window_return(px)


def dry_run(*, db=None, fixture=FIXTURE, api_key=None, write=True,
            append_candidates=False, candidates_path=None, sens_path=None, now_dt=None):
    now_dt = now_dt or datetime.datetime.now(datetime.timezone.utc)
    asof = now_dt.strftime("%Y-%m-%d")
    universe = yaml.safe_load(open(ROOT / "config" / "universe.yaml", encoding="utf-8")) or {}
    theme = (universe.get("themes") or {}).get("ai_power") or {}
    источник = theme.get("proxy_etf", "VRT.US")
    map_doc = json.load(open(fixture, encoding="utf-8"))
    map_doc.pop("_комментарий", None)

    con = _connect_ro(db)
    try:
        shock = _shock(con, источник, asof)
        event = {"событие": theme.get("event", "ai_power"),
                 "ключи": ["ai", "data center", "transformers", "power"],
                 "источник_шока": источник, "shock": shock, "дата": asof}
        protocol = WE.enumerate_event(
            event, map_doc=map_doc, api_key=api_key, con=con, universe=universe,
            allow_history_fetch=False,                      # БД read-only в разработке (Э5 включит)
            write_candidates=append_candidates, candidates_path=candidates_path,
            sens_path=sens_path, write=write, now_dt=now_dt)
    finally:
        con.close()
    protocol["режим"] = "Э4 mock end-to-end (dryrun, историческое событие ai_power)"
    return protocol


def main(argv=None):
    ap = argparse.ArgumentParser(description="Э4 mock end-to-end: перебор мира на ai_power")
    ap.add_argument("--no-api", action="store_true",
                    help="не использовать EODHD screener даже при наличии ключа (фолбэк БД)")
    ap.add_argument("--append-candidates", action="store_true",
                    help="(ж) записать пары в knowledge/edge_candidates.jsonl (по умолчанию НЕТ)")
    ap.add_argument("--no-write", action="store_true", help="протокол на диск не писать")
    ap.add_argument("--db", default=None,
                    help="путь к боевой БД (read-only); по умолчанию storage/oracle.db от корня "
                         "репо — в dev-worktree, где storage/ нет, укажи /home/oracle/oracle/storage/oracle.db")
    args = ap.parse_args(argv)
    api_key = None if args.no_api else os.environ.get("EODHD_API_KEY")
    p = dry_run(db=args.db, api_key=api_key, write=not args.no_write,
                append_candidates=args.append_candidates)
    print(f"[{p['run_id']}] {p['режим']}")
    print(f"  событие: {p['событие']['событие'][:80]}")
    print(f"  источник шока: {p['событие']['источник_шока']} shock={p['событие']['shock']}")
    if p.get("стоп_события"):
        print(f"  СТОП СОБЫТИЯ: {p['стоп_события']}")
        return 0
    print(f"  бюджет события: cap ${p['бюджет_события']['cap_usd']} · "
          f"потрачено ${p['бюджет_события']['spent_usd']} · LLM-вызовов {p['бюджет_события']['вызовов_llm']}")
    for s in p.get("сегменты_скрин", []):
        print(f"  сегмент[{s['порядок']}] {s['сегмент'][:45]:45} → {s['инструментов']:3d} "
              f"({s['источник_скрина']})")
    print(f"  ПЕРЕЧИСЛЕНО инструментов: {p['перечислено_инструментов']} "
          f"(цель {p['цель_инструментов']}); попыток {p['попыток']}/{p['кэп_попыток']}")
    print(f"  принято пар источник→инструмент: {p['принято_пар']}")
    print(f"  отсев_по_критериям: {json.dumps(p['отсев_по_критериям'], ensure_ascii=False)}")
    if p.get("кандидат_рёбра"):
        print(f"  (ж) кандидат-рёбра: {json.dumps(p['кандидат_рёбра'], ensure_ascii=False)[:200]}")
    print(f"  скоринг/ранг: {p['скоринг_ранг'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
