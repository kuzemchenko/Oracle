#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orchestrator/run.py — входная точка прогона воронки (интерактив и cron, §27).

Дирижёр по cron: вызывает чужие семейства через OpenRouter (П10), собирает поле суждений,
пишет протокол в journal/funnel_logs/. В Claude Code Дирижёр — сам Claude; этот скрипт —
воспроизводимый автономный путь.

Примеры:
    python3 orchestrator/run.py --mode mock              # сквозной прогон без сети/трат
    python3 orchestrator/run.py --mode live --theme brent
    python3 orchestrator/run.py --mode mock --agents b_technical,d_timeliness
"""
import sys
import json
import argparse
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.funnel import run_funnel  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Прогон воронки «Оракула» (§6)")
    ap.add_argument("--mode", choices=["auto", "live", "mock"], default="auto",
                    help="auto=live при наличии ключа, иначе mock")
    ap.add_argument("--theme", default="brent")
    ap.add_argument("--agents", default=None,
                    help="список id через запятую (по умолчанию все B/C/D/G)")
    ap.add_argument("--no-write", action="store_true", help="не писать протокол на диск")
    ap.add_argument("--field-only", action="store_true",
                    help="только поле суждений (этапы 1–2), без дебатов/синтеза")
    args = ap.parse_args(argv)

    agent_ids = args.agents.split(",") if args.agents else None
    p = run_funnel(theme=args.theme, mode=args.mode, agent_ids=agent_ids,
                   write=not args.no_write, full=not args.field_only)

    print(f"[{p['run_id']}] режим={p['mode']} тема={p['theme']}")
    print(f"  агентов ок: {p['agents_ok']}/{p['agents_total']} · "
          f"школ ок: {p['schools_ok']}/{p['schools_total']} · "
          f"кандидатов: {p['candidates_count']}")
    print(f"  школы с кандидатами: {', '.join(p['schools_with_candidates']) or '—'}")
    print(f"  агрегированная P: {p['контрфактический_протокол']['агрегированная_вероятность']}")
    print(f"  противоречий: {len(p['карта_противоречий'])} · "
          f"вето/П8: {len(p['процедурное_вето'])}")
    fr = p.get("воронка_отсева")
    if fr:
        print(f"  воронка §6: скан {fr['этап1_сырых_сигналов']}→FDR {fr['этап1_сигналов_после_FDR']} · "
              f"канд {fr['этап2_кандидатов']} → фильтр {fr['этап3_после_грубого_фильтра']} → "
              f"дебаты топ {fr['этап4_в_дебаты_топ']} → устояло {fr['этап5_устояло_после_дебатов']} → "
              f"выдано {fr['этап6_выдано_топ']}")
        print(f"  итог: {fr['вывод']}")
        synth = p.get("этап6_синтез") or {}
        for rep in synth.get("отчёты", []):
            pos = rep.get("позиция") or {}
            print(f"    • {rep['актив']} {rep.get('направление','')} балл={rep.get('балл')} "
                  f"драйвер={pos.get('макро_драйвер')} ${pos.get('amount_usd')}")
    if not args.no_write:
        print(f"  протокол: journal/funnel_logs/{p['run_id']}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
