# -*- coding: utf-8 -*-
"""mathlib/calibration/cascade_lags.py — эмпирическая калибровка ЦЕНОВОГО lead-lag рёбер
тектонических цепочек (knowledge/cascade_chains.yaml) на истории (§23.1 п.2, честная зона).

ЧЕСТНАЯ ГРАНИЦА (П16/§23.1):
  • Что МЕРИМ: ценовой lead-lag и ко-движение между торгуемыми узлами ребра — на синхронных
    рядах доходностей (код не помнит будущее → walk-forward легален). Это ВАЛИДИРУЕТ, что
    звенья реально связаны в ценах, и ловит редкий торгуемый lead-lag (недо-реакция).
  • Чего НЕ делаем: НЕ выдаём это за «экономический лаг каскада» (капекс→заказы→выручка,
    месяцы). На дневных ликвидных бумагах рынок упреждает → ценовой лаг обычно ≈0, что НЕ
    отменяет экономической цепочки. Поэтому гипотезы lag_days НЕ перезаписываем — дописываем
    measured price_leadlag рядом, с пометкой. Экономический лаг измерим лишь на операционных
    данных (выручка/заказы) — это честный пробел (нет истории фундаментала по узлам).

Множественность ребёр × горизонтов → FDR (Бенджамини–Хохберг), чтобы не принять шум за связь.
"""
import math

import numpy as np

from . import loader as L
from .causal import measure_pair
from ..fdr import benjamini_hochberg


def _pvalue(r, n):
    """Двусторонний p для корреляции (t-приближение). None при недостатке точек/|r|→1."""
    if r is None or n is None or n < 4:
        return None
    if abs(r) >= 1.0:
        return 0.0
    t = abs(r) * math.sqrt((n - 2) / (1.0 - r * r))
    # нормальное приближение к t при n>30 (у нас n десятки–тысячи)
    z = t
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
    return max(min(p, 1.0), 0.0)


def _weekly(series):
    """Грубый недельный Series (каждый 5-й торговый день) для лагов недельного горизонта."""
    s = L.Series.__new__(L.Series)
    s.symbol = series.symbol
    sel = slice(None, None, 5)
    s.dates = series.dates[sel]
    for a in ("open", "high", "low", "close", "adj", "volume"):
        setattr(s, a, getattr(series, a)[sel])
    return s


def _measure_edge(x_sym, y_sym, db, daily_max=15, weekly_max=12):
    """Ценовой lead-lag ребра x→y на дневном и недельном горизонте (adjusted_view)."""
    _, aligned = L.load_aligned([x_sym, y_sym], db=db)
    if x_sym not in aligned or y_sym not in aligned or len(aligned[x_sym]) < 80:
        return {"insufficient": True, "n": len(aligned.get(x_sym, []))}
    ax, ay = L.adjusted_view(aligned[x_sym]), L.adjusted_view(aligned[y_sym])
    out = {"insufficient": False, "x": x_sym, "y": y_sym}
    d = measure_pair(ax, ay, max_lag=daily_max)
    out["daily"] = d
    wx, wy = _weekly(ax), _weekly(ay)
    if len(wx) >= 60:
        out["weekly"] = measure_pair(wx, wy, max_lag=weekly_max)
    return out


def _rep_instrument(node):
    inst = node.get("instruments") or []
    return inst[0] if inst else None


def calibrate_chain(chain, db=L.DB):
    """Калибровка всех рёбер цепочки. Возвращает {chain_id, edges:[...], honesty_note}.

    Каждое ребро: гипотеза экономического лага (из карты, НЕ трогается) + measured ценовой
    lead-lag (daily/weekly, best_lag, r, CI, p, significant_fdr)."""
    nodes = {n["order"]: n for n in (chain.get("nodes") or [])}
    edges_in = chain.get("edges") or []
    raw = []
    for e in edges_in:
        xnode, ynode = nodes.get(e.get("from")), nodes.get(e.get("to"))
        x_sym = _rep_instrument(xnode) if xnode else None
        y_sym = _rep_instrument(ynode) if ynode else None
        rec = {"from": e.get("from"), "to": e.get("to"),
               "x": x_sym, "y": y_sym,
               "lag_hypothesis_days": e.get("lag_days"),  # экономическая гипотеза — НЕ трогаем
               "measured": None}
        if x_sym and y_sym:
            try:
                rec["measured"] = _measure_edge(x_sym, y_sym, db)
            except Exception as ex:  # noqa: BLE001
                rec["measured"] = {"insufficient": True, "error": type(ex).__name__}
        raw.append(rec)

    # FDR по всем измеренным дневным корреляциям ребёр
    pvals, idx = [], []
    for i, rec in enumerate(raw):
        m = (rec.get("measured") or {}).get("daily") or {}
        if not m.get("insufficient") and m.get("best_r") is not None:
            p = _pvalue(m["best_r"], m["n"])
            if p is not None:
                pvals.append(p)
                idx.append(i)
    significant = set()
    if pvals:
        rejected = benjamini_hochberg(pvals, q=0.10)["rejected"]
        for j, ok in zip(idx, rejected):
            if ok:
                significant.add(j)

    edges_out = []
    for i, rec in enumerate(raw):
        m = (rec.get("measured") or {}).get("daily") or {}
        edges_out.append({
            "from": rec["from"], "to": rec["to"], "x": rec["x"], "y": rec["y"],
            "lag_hypothesis_days": rec["lag_hypothesis_days"],
            "price_leadlag": None if m.get("insufficient", True) else {
                "best_lag_days": m.get("best_lag_days"),
                "best_r": m.get("best_r"),
                "best_r_ci95": m.get("best_r_ci95"),
                "contemporaneous_r": m.get("contemporaneous_r"),
                "n": m.get("n"),
                "significant_fdr": i in significant,
                "weekly_best_lag_days": ((rec["measured"].get("weekly") or {}) or {}).get("best_lag_days"),
                "weekly_best_r": ((rec["measured"].get("weekly") or {}) or {}).get("best_r"),
            },
        })
    return {
        "chain_id": chain.get("id"),
        "edges": edges_out,
        "method": "кросс-корреляция дневных/недельных log-доходностей (adjusted_close), "
                  "лаги ±15д/±12нед, Fisher CI, FDR q=0.10 по рёбрам",
        "honesty_note": "measured = ЦЕНОВОЙ lead-lag (валидирует связь узлов); это НЕ экономический "
                        "лаг каскада (капекс→выручка, месяцы) — он на дневных ценах не измерим "
                        "(рынок упреждает), требует операционных данных. Гипотезы lag_days сохранены.",
    }


def calibrate_all(db=L.DB, chains=None):
    from ..tectonic import load_chains
    chains = chains if chains is not None else load_chains()
    return [calibrate_chain(c, db=db) for c in chains]
