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
    python3 orchestrator/run.py --mode masked            # маскированные кейсы §23.2(б), gate ≥70%
    python3 orchestrator/run.py --mode masked --mock     # тот же набор, принудительно mock (дымовой)
    python3 orchestrator/run.py --mode ablation          # абляция вкладов §11.1 по журналам
"""
import sys
import json
import argparse
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.funnel import run_funnel  # noqa: E402


def _run_masked(args):
    from orchestrator import masked as M
    mode = "mock" if args.mock else "auto"
    s = M.run_masked(mode=mode, write=not args.no_write)
    if "ОТКАЗ_бюджет" in s:
        print(f"[{s['run_id']}] {s['вывод']}")
        return 1
    agg = s["агрегат"]
    print(f"[{s['run_id']}] режим={s['mode']} · {s['честность'][:60]}…")
    print(f"  кейсов: {agg['n_кейсов']} · зачтено: {agg['n_зачтено']} · "
          f"чисто П8: {agg['n_чисто_П8']}")
    print(f"  доля зачтённых: {agg['доля_зачтено']:.0%} (порог §24 {agg['порог_доли']:.0%}) → "
          f"{'GATE ПРОЙДЕН' if agg['gate_пройден'] else 'GATE НЕ пройден'}")
    print(f"  средний % рубрики (affirm): {agg['средний_процент_рубрики_affirm']}")
    for r in s["кейсы"]:
        mark = "✅" if r["case_passed"] else "❌"
        print(f"    {mark} {r['case_id']:30s} [{r['expected_stance']:6s}] "
              f"исход={r['verdict_outcome']} балл={r.get('rubric_pct')}% П8={r['p8_violations']}")
    if not args.no_write:
        print(f"  отчёт: reports/masked/{s['run_id']}.md")
    return 0 if agg["gate_пройден"] else 2


def _run_ablation(args):
    from orchestrator import ablation as A
    s = A.run_ablation(write=not args.no_write)
    print(f"[абляция §11.1] прогонов с контрфактами: {s['n_прогонов_всего']} "
          f"(live {s['n_прогонов_live']} / mock {s['n_прогонов_mock_тестовых']})")
    print(f"  связок исход↔контрфакт: {s['n_разрешённых_исходов_связок']}")
    print(f"  {s['вывод']}")
    print("  таблица влияния (drop-one, топ по |сдвигу|):")
    for r in s["таблица_влияния_drop_one"][:8]:
        print(f"    {r['agent']:28s} участий={r['n_участий']} "
              f"|сдвиг|={r['mean_abs_shift']} сдвиг={r['mean_shift']}")
    if not args.no_write:
        print("  предложения: journal/proposed_adjustments.md (применение — /apply-weights)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Прогон «Оракула»: воронка §6 / масккейсы §23.2 / абляция §11.1")
    ap.add_argument("--mode", choices=["auto", "live", "mock", "masked", "ablation"], default="auto",
                    help="auto/live/mock — воронка; masked — маскированные кейсы §23.2(б); "
                         "ablation — абляция вкладов §11.1")
    ap.add_argument("--mock", action="store_true",
                    help="для --mode masked: принудительно mock (дымовой тест конвейера)")
    ap.add_argument("--theme", default="brent")
    ap.add_argument("--agents", default=None,
                    help="список id через запятую (по умолчанию все B/C/D/G)")
    ap.add_argument("--no-write", action="store_true", help="не писать протокол на диск")
    ap.add_argument("--field-only", action="store_true",
                    help="только поле суждений (этапы 1–2), без дебатов/синтеза")
    args = ap.parse_args(argv)

    if args.mode == "masked":
        return _run_masked(args)
    if args.mode == "ablation":
        return _run_ablation(args)

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
