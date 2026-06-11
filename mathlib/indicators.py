# -*- coding: utf-8 -*-
"""mathlib/indicators.py — детерминированные индикаторы (MASTER_SPEC §4 «Технический»/«Своевременность», §23.1 п.1).

§21: индикаторы теханализа считает КОД, LLM лишь интерпретирует. Без числового тулкита
технический агент и волновик не включаются. Эти же функции питают тайминг-детектор §23.1 п.1
(пройденный ход в σ дневного хода, всплеск объёма) и калибровку порогов Недели 4.
Чистый numpy, без сети и без LLM.
"""
import numpy as np


def _arr(x):
    a = np.asarray(x, dtype=float)
    if a.ndim != 1:
        raise ValueError("ожидается 1-D ряд")
    return a


def returns(prices):
    """Простые доходности p[t]/p[t-1] - 1 (длина n-1)."""
    p = _arr(prices)
    if p.size < 2:
        raise ValueError("нужно ≥2 точек")
    return np.diff(p) / p[:-1]


def log_returns(prices):
    """Логарифмические доходности (длина n-1). Цены должны быть > 0."""
    p = _arr(prices)
    if p.size < 2:
        raise ValueError("нужно ≥2 точек")
    if np.any(p <= 0):
        raise ValueError("цены должны быть > 0 для log-доходностей")
    return np.diff(np.log(p))


def sma(x, n):
    """Простое скользящее среднее окном n (длина len-n+1)."""
    a = _arr(x)
    if n <= 0 or n > a.size:
        raise ValueError("некорректное окно n")
    c = np.cumsum(np.insert(a, 0, 0.0))
    return (c[n:] - c[:-n]) / n


def ema(x, n):
    """Экспоненциальное скользящее среднее, alpha = 2/(n+1); ema[0] = x[0] (длина len)."""
    a = _arr(x)
    if n <= 0:
        raise ValueError("некорректное окно n")
    alpha = 2.0 / (n + 1.0)
    out = np.empty_like(a)
    out[0] = a[0]
    for i in range(1, a.size):
        out[i] = alpha * a[i] + (1.0 - alpha) * out[i - 1]
    return out


def rolling_std(x, n, ddof=0):
    """Скользящее стандартное отклонение окном n (длина len-n+1)."""
    a = _arr(x)
    if n <= 1 or n > a.size:
        raise ValueError("некорректное окно n")
    out = np.empty(a.size - n + 1)
    for i in range(out.size):
        out[i] = a[i:i + n].std(ddof=ddof)
    return out


def realized_vol(prices, ddof=1):
    """Реализованная волатильность = std логарифмических доходностей (при дневном ряде — дневная σ)."""
    return float(log_returns(prices).std(ddof=ddof))


def zscore(x, n):
    """z-оценка ПОСЛЕДНЕЙ точки в окне n: (x[-1] - mean)/std. При нулевой дисперсии → 0.
    Питает тайминг-детектор §23.1 (всплеск объёма/хода в σ)."""
    a = _arr(x)
    if n <= 1 or n > a.size:
        raise ValueError("некорректное окно n")
    w = a[-n:]
    s = w.std(ddof=0)
    return 0.0 if s == 0 else float((a[-1] - w.mean()) / s)


def _rsi_value(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(prices, n=14):
    """RSI Уайлдера (длина len-n). Значения в [0,100]; рост без откатов → 100."""
    p = _arr(prices)
    if p.size < n + 1:
        raise ValueError("нужно ≥ n+1 точек")
    delta = np.diff(p)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:n].mean()
    avg_loss = loss[:n].mean()
    out = [_rsi_value(avg_gain, avg_loss)]
    for i in range(n, delta.size):
        avg_gain = (avg_gain * (n - 1) + gain[i]) / n
        avg_loss = (avg_loss * (n - 1) + loss[i]) / n
        out.append(_rsi_value(avg_gain, avg_loss))
    return np.array(out)


def atr(high, low, close, n=14):
    """Average True Range (Уайлдер, длина len-n). Все три ряда одной длины."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    if not (h.size == l.size == c.size):
        raise ValueError("high/low/close разной длины")
    if c.size < n + 1:
        raise ValueError("нужно ≥ n+1 точек")
    prev_close = c[:-1]
    tr = np.maximum.reduce([
        h[1:] - l[1:],
        np.abs(h[1:] - prev_close),
        np.abs(l[1:] - prev_close),
    ])
    a = tr[:n].mean()
    out = [a]
    for i in range(n, tr.size):
        a = (a * (n - 1) + tr[i]) / n
        out.append(a)
    return np.array(out)


def bollinger(prices, n=20, k=2.0):
    """Полосы Боллинджера: dict mid/upper/lower (каждая длиной len-n+1)."""
    a = _arr(prices)
    mid = sma(a, n)
    sd = rolling_std(a, n, ddof=0)
    return {"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd}


def macd(prices, fast=12, slow=26, signal=9):
    """MACD: dict macd/signal/hist (все длиной len)."""
    a = _arr(prices)
    if fast >= slow:
        raise ValueError("fast должен быть < slow")
    macd_line = ema(a, fast) - ema(a, slow)
    signal_line = ema(macd_line, signal)
    return {"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line}


def max_drawdown(equity):
    """Максимальная просадка кривой капитала (≤ 0; -0.2 = -20%). Питает лимит просадки §4 портфель."""
    e = _arr(equity)
    if e.size == 0:
        raise ValueError("пустой ряд")
    if np.any(e <= 0):
        raise ValueError("кривая капитала должна быть > 0")
    peak = np.maximum.accumulate(e)
    dd = (e - peak) / peak
    return float(dd.min())
