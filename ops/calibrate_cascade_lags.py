#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/calibrate_cascade_lags.py — калибровка ценового lead-lag рёбер тектонических цепочек
(§23.1 п.2, честная зона walk-forward). Пишет ГЕНЕРИРУЕМЫЙ артефакт, рукописную карту не трогает.

Запуск:
    python3 ops/calibrate_cascade_lags.py            # калибровать все цепочки → артефакт
    python3 ops/calibrate_cascade_lags.py --print     # только вывести отчёт, не писать
"""
import sys
import json
import argparse
import datetime
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib.calibration import cascade_lags as CL   # noqa: E402
from mathlib.calibration import operational_lags as OL  # noqa: E402
from mathlib import tectonic as TEC                   # noqa: E402

OUT = ROOT / "knowledge" / "cascade_lags_calibrated.yaml"
HEADER = ("# СГЕНЕРИРОВАНО ops/calibrate_cascade_lags.py (§23.1 п.2, честная зона walk-forward).\n"
          "# Ценовой lead-lag рёбер цепочек knowledge/cascade_chains.yaml. Правки руками будут "
          "перезаписаны.\n"
          "# ЧЕСТНОСТЬ: measured = ЦЕНОВОЙ lead-lag (валидирует связь узлов), НЕ экономический лаг\n"
          "# каскада (капекс→выручка, месяцы) — он на дневных ценах не измерим. lag_days в карте — гипотеза.\n\n")


def _load_fundamentals(symbols):
    """fundamentals raw_json по символам из oracle.db (для операционных лагов)."""
    import sqlite3
    con = sqlite3.connect(ROOT / "storage" / "oracle.db")
    out = {}
    try:
        for s in symbols:
            row = con.execute("SELECT raw_json FROM fundamentals WHERE symbol=?", (s,)).fetchone()
            if row and row[0]:
                try:
                    out[s] = json.loads(row[0])
                except json.JSONDecodeError:
                    pass
    finally:
        con.close()
    return out


def _report(price_results, op_results):
    op_by_id = {r["chain_id"]: r for r in op_results}
    for r in price_results:
        print(f"\n=== цепочка {r['chain_id']} ===")
        op_edges = {(e["from"], e["to"]): e for e in op_by_id.get(r["chain_id"], {}).get("edges", [])}
        for e in r["edges"]:
            pl = e.get("price_leadlag")
            op = (op_edges.get((e["from"], e["to"])) or {}).get("operational")
            price = (f"ценовой лаг={pl['best_lag_days']}д r={pl['best_r']} "
                     f"[{'ЗНАЧИМО' if pl['significant_fdr'] else 'нет'}]" if pl else "ценовой: нет данных")
            econ = (f"ЭКОН.лаг={op['lag_quarters']}кв (~{op['lag_quarters_days_approx']}д) r={op['r']} "
                    f"CI{op['r_ci95']} n={op['n']} [{'ЗНАЧИМО' if op['significant_fdr'] else 'нет'}; "
                    f"{op['power_note']}]" if op else "экон.: нет данных (мало кварталов)")
            print(f"  {e['x']}→{e['y']}: {price} | {econ} | гипотеза={e['lag_hypothesis_days']}д")
        print(f"  {r['honesty_note']}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="print_only", help="не писать артефакт")
    args = ap.parse_args(argv)

    if not (ROOT / "storage" / "oracle.db").exists():
        print("ОШИБКА: storage/oracle.db отсутствует — нужны котировки (data/eodhd.py)", file=sys.stderr)
        return 1

    results = CL.calibrate_all()
    # операционные (экономические) лаги: на квартальной выручке узлов из fundamentals
    chains = TEC.load_chains()
    op_results = []
    for ch in chains:
        syms = [i for n in (ch.get("nodes") or []) for i in (n.get("instruments") or [])]
        op_results.append(OL.calibrate_operational(ch, _load_fundamentals(syms)))
    _report(results, op_results)

    if not args.print_only:
        obj = {
            "version": 1,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "spec_ref": "§23.1 п.2; П16 (форвард для экон. лагов)",
            "method": results[0]["method"] if results else None,
            "honesty_note": results[0]["honesty_note"] if results else None,
            "chains_price_leadlag": results,
            "chains_operational_lag": op_results,
        }
        OUT.write_text(HEADER + yaml.safe_dump(obj, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        print(f"\n→ записано: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
