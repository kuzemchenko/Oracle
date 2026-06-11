# -*- coding: utf-8 -*-
"""Тесты детерминированных индикаторов (§4, §23.1 п.1, §21)."""
import sys
import pathlib

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import indicators as ind  # noqa: E402


def test_returns_and_log_returns():
    p = [100.0, 110.0, 99.0]
    assert ind.returns(p) == pytest.approx([0.1, -0.1])
    lr = ind.log_returns(p)
    assert lr == pytest.approx([np.log(1.1), np.log(99 / 110)])
    with pytest.raises(ValueError):
        ind.returns([100.0])
    with pytest.raises(ValueError):
        ind.log_returns([100.0, -5.0])


def test_sma():
    out = ind.sma([1, 2, 3, 4, 5], 3)
    assert out == pytest.approx([2.0, 3.0, 4.0])
    assert len(out) == 3
    with pytest.raises(ValueError):
        ind.sma([1, 2], 5)


def test_ema_first_equals_first_and_tracks():
    out = ind.ema([1, 2, 3, 4, 5], 3)
    assert out[0] == 1.0
    assert len(out) == 5
    # alpha=0.5: 0.5*2+0.5*1 = 1.5
    assert out[1] == pytest.approx(1.5)


def test_rolling_std_and_realized_vol():
    out = ind.rolling_std([1, 2, 3, 4], 2, ddof=0)
    assert out == pytest.approx([0.5, 0.5, 0.5])
    # постоянный рост в N раз → волатильность лог-доходностей 0
    assert ind.realized_vol([100, 110, 121, 133.1]) == pytest.approx(0.0, abs=1e-9)


def test_zscore():
    # окно [10,10,10,10,20]: mean=12, std=4 → (20-12)/4 = 2.0
    assert ind.zscore([10, 10, 10, 10, 20], 5) == pytest.approx(2.0)
    assert ind.zscore([5, 5, 5], 3) == 0.0  # нулевая дисперсия


def test_rsi_bounds_and_extremes():
    up = list(range(1, 30))                       # монотонный рост
    r = ind.rsi(up, n=14)
    assert np.all((r >= 0) & (r <= 100))
    assert r[-1] == pytest.approx(100.0)
    down = list(range(30, 1, -1))                 # монотонное падение
    rd = ind.rsi(down, n=14)
    assert rd[-1] == pytest.approx(0.0)
    assert len(r) == len(up) - 14


def test_atr_positive():
    n = 20
    high = [10 + i for i in range(n)]
    low = [9 + i for i in range(n)]
    close = [9.5 + i for i in range(n)]
    a = ind.atr(high, low, close, n=14)
    assert np.all(a > 0)
    assert len(a) == n - 14
    with pytest.raises(ValueError):
        ind.atr([1, 2], [1], [1, 2], n=1)


def test_bollinger_bands_order():
    prices = list(range(1, 30))
    b = ind.bollinger(prices, n=20, k=2.0)
    assert np.all(b["upper"] >= b["mid"])
    assert np.all(b["mid"] >= b["lower"])
    assert len(b["mid"]) == len(prices) - 20 + 1


def test_macd_shapes():
    prices = list(np.linspace(100, 120, 60))
    m = ind.macd(prices)
    assert len(m["macd"]) == len(prices)
    assert len(m["signal"]) == len(prices)
    assert len(m["hist"]) == len(prices)
    with pytest.raises(ValueError):
        ind.macd(prices, fast=26, slow=12)


def test_max_drawdown():
    eq = [100, 120, 90, 130]   # пик 120 → дно 90: -25%
    assert ind.max_drawdown(eq) == pytest.approx(-0.25)
    assert ind.max_drawdown([100, 101, 102]) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        ind.max_drawdown([100, -1])
