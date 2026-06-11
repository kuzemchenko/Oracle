# -*- coding: utf-8 -*-
"""mathlib/brier.py — Brier score и калибровка по корзинам (MASTER_SPEC П7, §7, §10.9).

П7: журнал всех вероятностных оценок; из событий с оценкой 70% должно сбываться ~70%.
Метрика — Brier score ПО КОРЗИНАМ вероятностей. calibration_band_pp питает денежные ворота §11
(калибровка ±10 п.п.) и KILL-критерий (хуже ±15 п.п.). Считается КОДОМ, не LLM (§21).
"""
import numpy as np


def _validate(probs, outcomes):
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if p.shape != y.shape:
        raise ValueError("probs и outcomes разной длины")
    if p.size == 0:
        raise ValueError("пустой вход — нет данных для Brier (П8)")
    if np.any((p < 0) | (p > 1)):
        raise ValueError("вероятности должны быть в [0,1]")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("исходы должны быть 0 или 1")
    return p, y


def brier_score(probs, outcomes):
    """Средний квадрат отклонения вероятности от исхода. 0 = идеал, ниже = лучше."""
    p, y = _validate(probs, outcomes)
    return float(np.mean((p - y) ** 2))


def calibration_table(probs, outcomes, n_bins=10):
    """Корзины [0,1] равной ширины (1.0 попадает в последнюю). Для каждой:
    n, mean_pred (средняя предсказанная вероятность), obs_freq (наблюдённая частота), gap=|pred-obs|.
    Пустые корзины: mean_pred/obs_freq/gap = None (нет данных, П8)."""
    p, y = _validate(probs, outcomes)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges, right=False) - 1, 0, n_bins - 1)
    table = []
    for b in range(n_bins):
        mask = idx == b
        n = int(mask.sum())
        row = {"lo": float(edges[b]), "hi": float(edges[b + 1]), "n": n}
        if n:
            mp = float(p[mask].mean())
            of = float(y[mask].mean())
            row.update(mean_pred=mp, obs_freq=of, gap=abs(mp - of))
        else:
            row.update(mean_pred=None, obs_freq=None, gap=None)
        table.append(row)
    return table


def calibration_band_pp(probs, outcomes, n_bins=10):
    """Максимальный разрыв калибровки в ПРОЦЕНТНЫХ ПУНКТАХ по непустым корзинам.
    Ворота §11: band ≤ 10 п.п. — норма; > 15 п.п. — KILL. None, если корзин с данными нет."""
    table = calibration_table(probs, outcomes, n_bins=n_bins)
    gaps = [r["gap"] for r in table if r["n"] and r["gap"] is not None]
    return max(gaps) * 100.0 if gaps else None


def reliability(probs, outcomes, n_bins=10):
    """Компонент надёжности разложения Brier (Murphy): взвешенный по корзинам средний квадрат
    (mean_pred - obs_freq). Меньше = лучше калибровка. Идеальная калибровка → 0."""
    p, _ = _validate(probs, outcomes)
    table = calibration_table(probs, outcomes, n_bins=n_bins)
    n = p.size
    rel = sum(r["n"] * (r["mean_pred"] - r["obs_freq"]) ** 2 for r in table if r["n"])
    return float(rel / n)
