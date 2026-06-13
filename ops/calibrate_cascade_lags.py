#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/calibrate_cascade_lags.py — калибровка ценового lead-lag рёбер тектонических цепочек
(§23.1 п.2, честная зона walk-forward). Пишет ГЕНЕРИРУЕМЫЙ артефакт, рукописную карту не трогает.

Запуск:
    python3 ops/calibrate_cascade_lags.py            # калибровать все цепочки → артефакт
    python3 ops/calibrate_cascade_lags.py --print     # только вывести отчёт, не писать
"""
import sys
import argparse
import datetime
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib.calibration import cascade_lags as CL   # noqa: E402

OUT = ROOT / "knowledge" / "cascade_lags_calibrated.yaml"
HEADER = ("# СГЕНЕРИРОВАНО ops/calibrate_cascade_lags.py (§23.1 п.2, честная зона walk-forward).\n"
          "# Ценовой lead-lag рёбер цепочек knowledge/cascade_chains.yaml. Правки руками будут "
          "перезаписаны.\n"
          "# ЧЕСТНОСТЬ: measured = ЦЕНОВОЙ lead-lag (валидирует связь узлов), НЕ экономический лаг\n"
          "# каскада (капекс→выручка, месяцы) — он на дневных ценах не измерим. lag_days в карте — гипотеза.\n\n")


def _report(results):
    for r in results:
        print(f"\n=== цепочка {r['chain_id']} ===")
        for e in r["edges"]:
            pl = e.get("price_leadlag")
            if not pl:
                print(f"  {e['x']}→{e['y']}: нет данных (короткая история)")
                continue
            sig = "ЗНАЧИМО(FDR)" if pl["significant_fdr"] else "не значимо"
            print(f"  {e['x']}→{e['y']}: ценовой лаг={pl['best_lag_days']}д "
                  f"r={pl['best_r']} CI{pl['best_r_ci95']} n={pl['n']} [{sig}] "
                  f"| недельный лаг={pl.get('weekly_best_lag_days')} r={pl.get('weekly_best_r')} "
                  f"| гипотеза экон.лага={e['lag_hypothesis_days']}д")
        print(f"  {r['honesty_note']}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="print_only", help="не писать артефакт")
    args = ap.parse_args(argv)

    if not (ROOT / "storage" / "oracle.db").exists():
        print("ОШИБКА: storage/oracle.db отсутствует — нужны котировки (data/eodhd.py)", file=sys.stderr)
        return 1

    results = CL.calibrate_all()
    _report(results)

    if not args.print_only:
        obj = {
            "version": 1,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "spec_ref": "§23.1 п.2; П16 (форвард для экон. лагов)",
            "method": results[0]["method"] if results else None,
            "honesty_note": results[0]["honesty_note"] if results else None,
            "chains": results,
        }
        OUT.write_text(HEADER + yaml.safe_dump(obj, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        print(f"\n→ записано: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
