# -*- coding: utf-8 -*-
"""ops/calibrate_sensitivities.py — ДРАЙВЕР калибровки каскадных чувствительностей (Этап 3, §23.1).

Генерирует (НЕ править руками — перезаписывается):
  • knowledge/cascade_sensitivities.yaml — пины бет переноса (где устойчиво) + провенанс
  • ops/reports/sensitivities/REPORT.md / report.json — человеко/машинный отчёт walk-forward

Что делает: берёт ЭМПИРИЧЕСКИЕ пары из knowledge/causal_links.yaml, грузит синхронные ряды,
калибрует бету узла к источнику в ОБЕ стороны walk-forward (mathlib.calibration.sensitivity),
пинит median по фолдам там, где знак согласован и перенос установлен; иначе «форвард-онли» (П8).
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import cascade as CAS                       # noqa: E402
from mathlib.calibration import sensitivity as SEN       # noqa: E402
from mathlib.calibration import loader as LD             # noqa: E402

OUT_YAML = ROOT / "knowledge" / "cascade_sensitivities.yaml"
REPORTS = ROOT / "ops" / "reports" / "sensitivities"
CAUSAL = ROOT / "knowledge" / "causal_links.yaml"

HEADER = (
    "# СГЕНЕРИРОВАНО ops/calibrate_sensitivities.py (Этап 3, §23.1 честная зона walk-forward).\n"
    "# Правки руками будут перезаписаны при следующей калибровке. Бета — историческая\n"
    "# чувствительность доходностей узла к источнику; вход движка mathlib/cascade.py.\n"
)


def _empirical_pairs(causal):
    seen, pairs = set(), []
    for ln in (causal.get("links") or []):
        if ln.get("source") != "empirical":
            continue
        pr = [str(x) for x in (ln.get("pair") or [])]
        if len(pr) != 2:
            continue
        key = tuple(sorted(pr))
        if key in seen:
            continue
        seen.add(key)
        pairs.append({"pair": pr, "lag_days": int(ln.get("lag_days") or 0),
                      "mechanism": ln.get("mechanism")})
    return pairs


def calibrate(db=None):
    causal = yaml.safe_load(open(CAUSAL, encoding="utf-8")) or {}
    pairs = _empirical_pairs(causal)
    records = []
    for p in pairs:
        a, b = p["pair"]
        _, series = LD.load_aligned([a, b], db=db) if db else LD.load_aligned([a, b])
        if a not in series or b not in series or series[a].adj.size == 0 or series[b].adj.size == 0:
            records.append({"pair": p["pair"], "pinned": None, "beta_pinned": None,
                            "provenance": "нет данных (П8): нет синхронных рядов"})
            continue
        ra, rb = CAS.log_returns(series[a].adj), CAS.log_returns(series[b].adj)
        lag = p["lag_days"]
        # обе стороны: бета(b←a) и бета(a←b) — направленны, даже если корреляция симметрична
        rec_ab = SEN.calibrate_pair_sensitivity(ra, rb, lag=lag)
        rec_ba = SEN.calibrate_pair_sensitivity(rb, ra, lag=lag)
        records.append({"источник": a, "узел": b, "mechanism": p["mechanism"], **rec_ab})
        records.append({"источник": b, "узел": a, "mechanism": p["mechanism"], **rec_ba})
    return {"train_window": _train_window(), "n_pairs": len(pairs),
            "n_pinned": sum(1 for r in records if r.get("pinned")),
            "honesty_note": ("эмпирические лаги дневных ETF = 0 (синхронны); каскадные лаги "
                             "недель/месяцев не измеримы на ETF — калибруются форвардом. Бета "
                             "пинится только при устойчивости по фолдам (П8)."),
            "sensitivities": records}


def _train_window():
    try:
        syms = LD.list_symbols()
        _, series = LD.load_aligned(syms[:1]) if syms else (None, {})
        if series:
            d = next(iter(series.values())).dates
            return {"from": str(d[0]), "to": str(d[-1]), "n": int(d.size)}
    except Exception:  # noqa: BLE001
        pass
    return None


def write(result):
    REPORTS.mkdir(parents=True, exist_ok=True)
    OUT_YAML.write_text(HEADER + "\n" + yaml.safe_dump(result, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    (REPORTS / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    lines = ["# Калибровка каскадных чувствительностей (Этап 3, §23.1)", "",
             f"_пар: {result['n_pairs']} · запинено бет: {result['n_pinned']}_", "",
             f"{result['honesty_note']}", "", "| источник→узел | lag | β пин | β fullsample | R² | провенанс |",
             "|---|---|---|---|---|---|"]
    for r in result["sensitivities"]:
        src, nd = r.get("источник", "?"), r.get("узел", "?")
        bp = r.get("beta_pinned")
        lines.append(f"| {src}→{nd} | {r.get('lag','-')} | {bp if bp is not None else '—'} "
                     f"| {r.get('beta_fullsample','—')} | {r.get('r2_fullsample','—')} "
                     f"| {(r.get('provenance') or '')[:70]} |")
    (REPORTS / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    res = calibrate()
    write(res)
    print(f"[калибровка чувствительностей] пар {res['n_pairs']}, запинено {res['n_pinned']}")
    print(f"  → {OUT_YAML}")
    print(f"  → {REPORTS}/REPORT.md")
