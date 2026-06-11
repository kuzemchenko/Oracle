# -*- coding: utf-8 -*-
"""mathlib/calibration/backgrounds.py — фоновые дисперсии метрик скана (§23.1 п.6, §6).

§6: при 200–500 проверках в день закономерности в шуме гарантированы → FDR-контроль.
Чтобы посчитать p-value наблюдаемой аномалии (для процедуры Бенджамини–Хохберга в
mathlib.fdr), нужен ФОН — распределение метрики на спокойной истории. Здесь оно и считается
детерминированно по ценам/объёмам. Частоты СЛОВ требуют длинной истории новостей, которой
ещё нет (1 месяц), — для них фон честно помечается «нет данных» (П8) вызывающим кодом.

Метрики ядра:
  ret      — дневная log-доходность (аномалия = ценовой шок)
  absret   — |log-доходность| (аномалия величины хода безотносительно знака)
  dvol     — дневное изменение log-объёма (аномалия активности = «толпа вошла»)
"""
import numpy as np
from ..indicators import log_returns


def _clean(x):
    a = np.asarray(x, dtype=float)
    return a[np.isfinite(a)]


def metric_series(series, metric):
    """Построить выбранную метрику из loader.Series."""
    close = series.close
    if metric == "ret":
        return log_returns(close[close > 0]) if np.any(close > 0) else np.array([])
    if metric == "absret":
        r = log_returns(close[close > 0]) if np.any(close > 0) else np.array([])
        return np.abs(r)
    if metric == "dvol":
        v = series.volume.astype(float)
        v = np.where(v > 0, v, np.nan)
        lv = np.log(v)
        d = np.diff(lv)
        return d[np.isfinite(d)]
    raise ValueError(f"неизвестная метрика {metric!r}")


def background(values):
    """Сводка фонового распределения метрики (для p-value скана §6).

    Возвращает робастные параметры: mean, std (фоновая дисперсия — то, что §23.1 п.6
    требует «посчитать на истории»), median, MAD-σ (1.4826·MAD — устойчив к выбросам),
    и эмпирические хвостовые квантили для непараметрического p-value.
    """
    a = _clean(values)
    if a.size < 30:
        return {"n": int(a.size), "insufficient": True}
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med)))
    return {
        "n": int(a.size),
        "insufficient": False,
        "mean": float(a.mean()),
        "std": float(a.std(ddof=1)),
        "var": float(a.var(ddof=1)),
        "median": med,
        "mad_sigma": float(1.4826 * mad),
        "q95": float(np.quantile(a, 0.95)),
        "q99": float(np.quantile(a, 0.99)),
        "q01": float(np.quantile(a, 0.01)),
        "q05": float(np.quantile(a, 0.05)),
    }


def empirical_p_two_sided(value, background_values):
    """Двусторонний эмпирический p-value наблюдения относительно фона.

    p = (1 + #{|x-med| >= |value-med|}) / (n+1)  — оценка с +1 (без нулевых p).
    Непараметрично: не предполагает нормальность хвостов (доходности тяжелохвостые).
    """
    a = _clean(background_values)
    if a.size == 0:
        raise ValueError("пустой фон")
    med = np.median(a)
    dist = np.abs(a - med)
    obs = abs(value - med)
    ge = int(np.sum(dist >= obs))
    return (1 + ge) / (a.size + 1)


def background_grid(series_map, metrics=("ret", "absret", "dvol"), train_slice=None):
    """Фоновые сводки по {symbol: Series} × метрики (опц. только на train-срезе)."""
    out = {}
    for sym, ser in series_map.items():
        out[sym] = {}
        for m in metrics:
            vals = metric_series(ser, m)
            if train_slice is not None:
                vals = vals[train_slice]
            out[sym][m] = background(vals)
    return out
