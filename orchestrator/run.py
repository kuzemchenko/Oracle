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


def _run_calibrate(args):
    from orchestrator import calibrate as CAL
    mode = "mock" if args.mock else "auto"
    s = CAL.run_calibrate(mode=mode, write=not args.no_write)
    if "ОТКАЗ" in s:
        print(f"[{s['run_id']}] {s['ОТКАЗ']}")
        return 1
    print(f"[{s['run_id']}] режим={s['mode']} · калибровка §17.3")
    print(f"  сгенерировано: {s['сгенерировано']} · разрешимо §9: {s['разрешимо_§9']} · "
          f"запечатано: {s['запечатано']} ({s['запечатывание']})")
    print(f"  всего в журнале: {s['всего_в_журнале']} · разрешено исходов: {s['разрешено_исходов']} · "
          f"Brier: {s['текущий_brier']} · до ворот 270: {s['до_ворот_270']}")
    print(f"  честность: {s['честность']}")
    return 0


def _run_resolve(args):
    from orchestrator import resolve as RES
    s = RES.run_resolve(write=not args.no_write)
    print(f"[resolve §10.10] прогнозов в журнале: {s['прогнозов_в_журнале']} · "
          f"сверено сейчас: {s['сверено_сейчас']} · ещё pending: {s['ещё_pending']}")
    print(f"  всего исходов: {s['всего_исходов']} · Brier: {s['brier']} · "
          f"калибровка band: {s['калибровка_band_пп']} п.п. · до ворот 270: {s['до_ворот_270']}")
    if s["ошибок"]:
        print(f"  ⚠ ошибок сверки: {s['ошибок']}")
    return 0


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


def _run_multi(args):
    from orchestrator.multi_event import run_multi_event
    mode = "mock" if args.mock else "auto"
    p = run_multi_event(mode=mode, k=args.k, write=not args.no_write)
    print(f"[{p['run_id']}] МУЛЬТИ-СОБЫТИЕ · {p['mode']}")
    print("Ранжирование событий по тектонике:")
    for e in p["ранжирование_событий"]:
        t = e.get("tectonic")
        tag = f" T={t['T']}→{ (t['далёкий_узел'] or {}).get('instruments') }" if t else ""
        anc = "" if e["anchorable"] else " (не якоримо)"
        print(f"  {e['score']:.2f}  {e['id']}{tag}{anc}")
    print(f"Глубоко проанализировано: {', '.join(p['глубоко_проанализировано'])}")
    for pe in p["по_событиям"]:
        print(f"  • {pe['событие']}: кандидатов {pe['кандидатов']} → выдано {pe['выдано']} ({pe['итог']})")
    if p["обнаруженные_кластеры_новостей"]:
        print("Обнаружены кластеры новостей (для регистрации):")
        for cl in p["обнаруженные_кластеры_новостей"][:4]:
            print(f"  ~{cl['salience']} {cl['keywords']}")
    print(f"Объединённая выдача: {p['объединённая_выдача_топ3'] or 'идей нет (§6)'}")
    print(f"Итог: {p['итог']}")
    if not args.no_write:
        print(f"Протокол: journal/funnel_logs/{p['run_id']}.md")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Прогон «Оракула»: воронка §6 / масккейсы §23.2 / абляция §11.1")
    ap.add_argument("--mode",
                    choices=["auto", "live", "mock", "funnel", "theme", "multi", "calibrate",
                             "resolve", "masked", "ablation"],
                    default="auto",
                    help="auto/live/mock/funnel — полная воронка §6; theme — тематический режим §17.2 "
                         "(полный цикл по --asset, запечатывание прогноза); calibrate — калибровка §17.3; "
                         "resolve — сверка исходов §10.10; masked — маскированные кейсы §23.2(б); "
                         "ablation — абляция вкладов §11.1")
    ap.add_argument("--mock", action="store_true",
                    help="для masked/calibrate: принудительно mock (дымовой тест конвейера, без seal)")
    ap.add_argument("--theme", default="brent")
    ap.add_argument("--asset", default=None, help="актив тематического режима (алиас --theme; §17.2)")
    ap.add_argument("--agents", default=None,
                    help="список id через запятую (по умолчанию все B/C/D/G)")
    ap.add_argument("--no-write", action="store_true", help="не писать протокол на диск")
    ap.add_argument("--field-only", action="store_true",
                    help="только поле суждений (этапы 1–2), без дебатов/синтеза")
    ap.add_argument("--k", type=int, default=3, help="мульти-режим: сколько топ-событий анализировать")
    args = ap.parse_args(argv)

    if args.mode == "masked":
        return _run_masked(args)
    if args.mode == "multi":
        return _run_multi(args)
    if args.mode == "ablation":
        return _run_ablation(args)
    if args.mode == "calibrate":
        return _run_calibrate(args)
    if args.mode == "resolve":
        return _run_resolve(args)

    # funnel/theme — синонимы боевого прогона: 'auto' (live при ключе, иначе mock).
    # theme §17.2 — тематический фокус на --asset (по умолчанию brent) с полным циклом и
    # запечатыванием прогноза; funnel §17.1 — свободная генерация (тоже стартует с темы brent).
    theme = args.asset or args.theme
    # --mock форсит mock в ЛЮБОМ режиме (защита от непреднамеренных live-трат: раньше для
    #   funnel/theme флаг молча игнорировался, и 'auto'→live при наличии ключа).
    funnel_mode = "mock" if args.mock else ("auto" if args.mode in ("funnel", "theme") else args.mode)

    agent_ids = args.agents.split(",") if args.agents else None
    p = run_funnel(theme=theme, mode=funnel_mode, agent_ids=agent_ids,
                   write=not args.no_write, full=not args.field_only,
                   theme_focused=(args.mode == "theme"))

    print(f"[{p['run_id']}] режим={p['mode']} тема={p['theme']}")
    # Протокол-отказ (бюджет §24 / нет данных) не содержит полей прогона — печатаем причину и выходим.
    if "agents_total" not in p:
        ref = p.get("ОТКАЗ_бюджет") or p.get("ОТКАЗ_тема") or p.get("ОТКАЗ") or {}
        print(f"  ОТКАЗ: {ref.get('reason') or p.get('следующий_шаг') or 'прогон не выполнен'}")
        if p.get("следующий_шаг"):
            print(f"  следующий шаг: {p['следующий_шаг']}")
        return 0
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
