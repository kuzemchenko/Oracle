# -*- coding: utf-8 -*-
"""База «случайного взгляда» для §9-вопроса (mathlib/base_odds.py, перепроверка №2 03.11)."""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from mathlib import base_odds as BO   # noqa: E402


def test_at_the_money_is_half():
    assert abs(BO.base_probability(100, 100, 0.02, 20, "above") - 0.5) < 1e-12
    assert abs(BO.base_probability(100, 100, 0.02, 20, "below") - 0.5) < 1e-12


def test_known_value_one_sigma():
    # порог ровно на +1σ горизонта: P(выше) = 1 − Φ(1) ≈ 0.158655
    s, days = 0.02, 25
    thr = 100 * math.exp(s * math.sqrt(days))
    assert abs(BO.base_probability(100, thr, s, days, "above") - 0.158655) < 1e-5
    assert abs(BO.base_probability(100, thr, s, days, "below") - 0.841345) < 1e-5


def test_farther_threshold_is_rarer_and_longer_horizon_is_likelier():
    near = BO.base_probability(40, 44, 0.03, 15, "above")
    far = BO.base_probability(40, 48, 0.03, 15, "above")
    longer = BO.base_probability(40, 48, 0.03, 40, "above")
    assert near > far and longer > far


def test_bad_input_is_none():
    assert BO.base_probability(0, 10, 0.02, 10, "above") is None
    assert BO.base_probability(10, 10, 0, 10, "above") is None
    assert BO.base_probability(10, 10, 0.02, 0, "above") is None
    assert BO.base_probability(10, 10, 0.02, 10, "sideways") is None
    assert BO.base_probability("x", 10, 0.02, 10, "above") is None


def test_daily_sigma_and_trading_days():
    assert BO.daily_sigma([1, 2, 3]) is None                     # мало данных
    flat = [100.0] * 70
    assert BO.daily_sigma(flat) is None                          # нулевая дисперсия
    alt = [100 * (1.01 if i % 2 else 1.0) for i in range(70)]
    s = BO.daily_sigma(alt)
    assert s is not None and 0.009 < s < 0.011
    assert BO.trading_days("2026-11-02", "2026-11-09") == 5      # пн → пн: 5 рабочих
    assert BO.trading_days("2026-10-30", "2026-11-25") == 18     # пт 30.10 → ср 25.11



def test_poisson_binomial_tail():
    assert BO.poisson_binomial_sf(0, [0.3, 0.5]) == 1.0
    assert abs(BO.poisson_binomial_sf(2, [0.3, 0.5]) - 0.15) < 1e-12        # обе сбылись
    assert abs(BO.poisson_binomial_sf(1, [0.3, 0.5]) - (1 - 0.7 * 0.5)) < 1e-12
    assert BO.poisson_binomial_sf(3, [0.3, 0.5]) == 0.0
    # совпадает с биномом при равных p: P(X≥8 | n=10, p=0.5) = 56/1024
    assert abs(BO.poisson_binomial_sf(8, [0.5] * 10) - 56 / 1024) < 1e-12
    assert BO.poisson_binomial_sf(1, [1.5]) is None
