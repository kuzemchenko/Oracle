# -*- coding: utf-8 -*-
"""mathlib/calibration/causal.py — эмпирические лаги причинных связей (§23.1 п.2).

§23.1 п.2: «измерить на истории фактические задержки … → knowledge/causal_links с
эмпирическими лагами и доверительными интервалами». Где у нас ЕСТЬ оба инструмента
(нефть, медь, индекс сырья, акции) — лаг измеряется по кросс-корреляции дневных доходностей
на синхронных рядах (loader.load_aligned). Где инструмента в универсуме нет — связь
помечается source=domain_knowledge, calibrated=false (ждёт форвард-проверки, П16/§23).

Лаг по знаку: положительный лаг L означает «x опережает y на L дней» (доходность x
сегодня коррелирует с доходностью y через L дней).
"""
import numpy as np
from ..indicators import log_returns


def _fisher_ci(r, n, alpha=0.05):
    if n <= 3 or not np.isfinite(r) or abs(r) >= 1:
        return (None, None)
    z = np.arctanh(r)
    se = 1.0 / np.sqrt(n - 3)
    zc = 1.959963984540054  # 97.5-й перцентиль N(0,1)
    return (float(np.tanh(z - zc * se)), float(np.tanh(z + zc * se)))


def lead_lag(ret_x, ret_y, max_lag=15):
    """Кросс-корреляция доходностей по лагам −max_lag..+max_lag.

    Возвращает list of dict(lag, r, n) и отдельно лучший лаг по |r|.
    """
    x = np.asarray(ret_x, float)
    y = np.asarray(ret_y, float)
    if x.size != y.size:
        raise ValueError("ряды доходностей разной длины")
    res = []
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            a, b = x[:x.size - lag], y[lag:]
        else:
            a, b = x[-lag:], y[:y.size + lag]
        mask = np.isfinite(a) & np.isfinite(b)
        a, b = a[mask], b[mask]
        if a.size >= 30 and a.std() > 0 and b.std() > 0:
            r = float(np.corrcoef(a, b)[0, 1])
        else:
            r = float("nan")
        res.append({"lag": lag, "r": r, "n": int(a.size)})
    valid = [d for d in res if np.isfinite(d["r"])]
    best = max(valid, key=lambda d: abs(d["r"])) if valid else None
    return res, best


def measure_pair(series_x, series_y, max_lag=15):
    """Измерить связь между двумя ВЫРОВНЕННЫМИ Series (одинаковые даты).

    Возвращает dict с best_lag, корреляцией, доверительным интервалом (Fisher) и
    контемпоральной (lag=0) корреляцией.
    """
    rx = log_returns(np.where(series_x.close > 0, series_x.close, np.nan)) \
        if np.any(series_x.close > 0) else np.array([])
    ry = log_returns(np.where(series_y.close > 0, series_y.close, np.nan)) \
        if np.any(series_y.close > 0) else np.array([])
    if rx.size != ry.size or rx.size < 60:
        return {"insufficient": True, "n": int(min(rx.size, ry.size))}
    table, best = lead_lag(rx, ry, max_lag)
    contemp = next(d for d in table if d["lag"] == 0)
    lo, hi = _fisher_ci(best["r"], best["n"]) if best else (None, None)
    return {
        "insufficient": False,
        "x": series_x.symbol, "y": series_y.symbol,
        "best_lag_days": best["lag"],
        "best_r": round(best["r"], 4),
        "best_r_ci95": [None if lo is None else round(lo, 4),
                        None if hi is None else round(hi, 4)],
        "contemporaneous_r": round(contemp["r"], 4),
        "n": best["n"],
        "max_lag_scanned": max_lag,
    }
