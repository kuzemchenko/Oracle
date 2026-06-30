# -*- coding: utf-8 -*-
"""Тесты детерминированной эмпирической base_rate (mathlib/base_rate.py, F2#17, Инв#6)."""
import math

import numpy as np

from mathlib import base_rate as BR


def _series_with_drift(n, mu, sigma, seed=0):
    r = np.random.default_rng(seed)
    rets = r.normal(mu, sigma, n)
    return list(100.0 * np.exp(np.cumsum(rets)))


def test_unknown_direction_returns_none():
    px = _series_with_drift(400, 0.0, 0.01)
    rate, n = BR.empirical_directional_base_rate(px, direction=None)
    assert rate is None and n == 0
    rate2, _ = BR.empirical_directional_base_rate(px, direction="вбок")
    assert rate2 is None


def test_short_history_no_data():
    px = _series_with_drift(40, 0.0, 0.01)        # < MIN_VOL_OBS + h
    rate, n = BR.empirical_directional_base_rate(px, direction="лонг")
    assert rate is None and n == 0


def test_upward_drift_higher_base_rate_for_long():
    up = _series_with_drift(600, 0.004, 0.01, seed=1)     # сильный дрейф вверх
    long_rate, n = BR.empirical_directional_base_rate(up, direction="лонг")
    short_rate, _ = BR.empirical_directional_base_rate(up, direction="шорт")
    assert n > 0
    assert 0.0 <= long_rate <= 1.0 and 0.0 <= short_rate <= 1.0
    assert long_rate > short_rate          # при дрейфе вверх лонг-ход случается чаще шорт-хода


def test_direction_synonyms_map():
    assert BR._dir_sign("long") == 1 and BR._dir_sign("BUY") == 1
    assert BR._dir_sign("short") == -1 and BR._dir_sign("Падение") == -1
    assert BR._dir_sign("нет") == 0


def test_rate_is_a_probability():
    px = _series_with_drift(500, 0.0, 0.015, seed=3)
    rate, n = BR.empirical_directional_base_rate(px, direction="лонг")
    assert n == len(px) - BR.H_DEFAULT
    assert 0.0 <= rate <= 1.0
