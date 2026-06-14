# -*- coding: utf-8 -*-
"""mathlib/calibration/operational_lags.py — ЭКОНОМИЧЕСКИЙ лаг каскада на ОПЕРАЦИОННЫХ данных
(долг №5, §23.1 п.2). Меряет lead-lag КВАРТАЛЬНОЙ выручки (YoY-рост) между узлами цепочки —
то, что ценовой lead-lag поймать не может (на дневных ценах =0, рынок упреждает).

Источник — EODHD fundamentals (Financials.Income_Statement.quarterly.totalRevenue), уже в БД.
ЧЕСТНОСТЬ (П8/П16): кварталов мало → N маленькое, CI широкий, мощность низкая. Возвращаем N и
CI всегда; FDR по рёбрам. Это эмпирическая оценка, не истина — слабые связи честно помечаем.
"""
import math

import numpy as np

from .causal import _fisher_ci
from ..fdr import benjamini_hochberg

MIN_GROWTH_POINTS = 6   # минимум перекрывающихся YoY-точек, иначе «недостаточно»


def revenue_series(fund_dict):
    """Список (квартал_дата, выручка) по возрастанию из EODHD fundamentals. [] если нет."""
    fin = ((fund_dict or {}).get("Financials") or {}).get("Income_Statement") or {}
    q = fin.get("quarterly") or {}
    out = []
    for d in sorted(q.keys()):
        v = q[d].get("totalRevenue")
        try:
            fv = float(v)
            out.append((d, fv))
        except (TypeError, ValueError):
            continue
    return out


def yoy_growth(series):
    """YoY-рост выручки (снимает сезонность): growth[i]=rev[i]/rev[i-4]-1, ключ — дата квартала."""
    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    out = {}
    for i in range(4, len(vals)):
        if vals[i - 4] and vals[i - 4] != 0:
            out[dates[i]] = vals[i] / vals[i - 4] - 1.0
    return out


def _pvalue(r, n):
    if r is None or n is None or n < 4:
        return None
    if abs(r) >= 1.0:
        return 0.0
    t = abs(r) * math.sqrt((n - 2) / (1.0 - r * r))
    return max(min(2.0 * (1.0 - 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))), 1.0), 0.0)


def lead_lag_quarters(growth_x, growth_y, max_lag=6):
    """Кросс-корреляция YoY-роста по КВАРТАЛЬНЫМ лагам. Положит. лаг = x опережает y на k кв.

    growth_x/y — dict {дата: рост}. Возвращает best {lag, r, n} по |r| ИЛИ None при нехватке."""
    common = sorted(set(growth_x) & set(growth_y))
    if len(common) < MIN_GROWTH_POINTS:
        return None
    gx = np.array([growth_x[d] for d in common], dtype=float)
    gy = np.array([growth_y[d] for d in common], dtype=float)
    best = None
    for lag in range(0, max_lag + 1):
        if lag == 0:
            a, b = gx, gy
        else:
            a, b = gx[:-lag], gy[lag:]      # x[t] vs y[t+lag] → x опережает y на lag
        if a.size < 4 or np.std(a) == 0 or np.std(b) == 0:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if r == r and (best is None or abs(r) > abs(best["r"])):
            best = {"lag_quarters": lag, "r": round(r, 4), "n": int(a.size)}
    return best


def calibrate_operational(chain, fundamentals_by_symbol, max_lag=6):
    """Экономический лаг по рёбрам цепочки на квартальной выручке. fundamentals_by_symbol:
    {symbol: распарсенный fundamentals dict}. FDR по рёбрам."""
    nodes = {n["order"]: n for n in (chain.get("nodes") or [])}
    growth = {}
    for sym, fund in fundamentals_by_symbol.items():
        g = yoy_growth(revenue_series(fund))
        if g:
            growth[sym] = g

    raw = []
    for e in chain.get("edges") or []:
        xs = (nodes.get(e["from"]) or {}).get("instruments") or []
        ys = (nodes.get(e["to"]) or {}).get("instruments") or []
        x, y = (xs[0] if xs else None), (ys[0] if ys else None)
        rec = {"from": e["from"], "to": e["to"], "x": x, "y": y,
               "lag_hypothesis_days": e.get("lag_days"), "operational": None}
        if x in growth and y in growth:
            rec["operational"] = lead_lag_quarters(growth[x], growth[y], max_lag)
        raw.append(rec)

    pvals, idx = [], []
    for i, rec in enumerate(raw):
        op = rec.get("operational")
        if op:
            p = _pvalue(op["r"], op["n"])
            if p is not None:
                pvals.append(p); idx.append(i)
    significant = set()
    if pvals:
        rej = benjamini_hochberg(pvals, q=0.10)["rejected"]
        significant = {j for j, ok in zip(idx, rej) if ok}

    edges = []
    for i, rec in enumerate(raw):
        op = rec.get("operational")
        if op:
            lo, hi = _fisher_ci(op["r"], op["n"])
            op = {**op, "r_ci95": [None if lo is None else round(lo, 4),
                                   None if hi is None else round(hi, 4)],
                  "lag_quarters_days_approx": op["lag_quarters"] * 91,
                  "significant_fdr": i in significant,
                  "power_note": "низкая мощность (мало кварталов)" if op["n"] < 12 else "приемлемо"}
        edges.append({"from": rec["from"], "to": rec["to"], "x": rec["x"], "y": rec["y"],
                      "lag_hypothesis_days": rec["lag_hypothesis_days"], "operational": op})
    return {
        "chain_id": chain.get("id"), "edges": edges,
        "method": "lead-lag КВАРТАЛЬНОГО YoY-роста выручки (EODHD Financials), лаги 0..6 кв, "
                  "Fisher CI, FDR q=0.10; снимает сезонность, ловит ЭКОНОМИЧЕСКИЙ лаг переноса",
        "honesty_note": "кварталов мало → низкая мощность; это эмпирическая оценка экон. лага "
                        "(в отличие от ценового lead-lag), подтверждать форвардом (П16).",
    }
