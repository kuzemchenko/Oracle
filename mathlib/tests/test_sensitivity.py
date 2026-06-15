# -*- coding: utf-8 -*-
"""Тесты walk-forward калибровки чувствительностей (mathlib/calibration/sensitivity.py, Этап 3)."""
import numpy as np

from mathlib.calibration import sensitivity as SEN


def test_stable_beta_is_pinned():
    r = np.random.default_rng(0)
    src = r.normal(0, 0.02, 1500)
    node = 1.3 * src + r.normal(0, 0.002, 1500)      # устойчивая бета 1.3
    rec = SEN.calibrate_pair_sensitivity(src, node, lag=0)
    assert rec["pinned"] is True
    assert abs(rec["beta_pinned"] - 1.3) < 0.05
    assert rec["sign_consistent"] is True
    assert "ПИН" in rec["provenance"]


def test_noise_is_not_pinned():
    r = np.random.default_rng(1)
    src = r.normal(0, 0.02, 1500)
    node = r.normal(0, 0.02, 1500)                   # независимый шум — переноса нет
    rec = SEN.calibrate_pair_sensitivity(src, node, lag=0)
    assert rec["pinned"] is False
    assert rec["beta_pinned"] is None
    assert "НЕ ПИНИТСЯ" in rec["provenance"]


def test_short_history_no_data():
    r = np.random.default_rng(2)
    src = r.normal(0, 0.02, 300)                     # < train+test
    node = 1.3 * src
    rec = SEN.calibrate_pair_sensitivity(src, node, lag=0)
    assert rec["pinned"] is None
    assert "нет данных" in rec["provenance"]


def test_lag_calibration():
    r = np.random.default_rng(3)
    src = r.normal(0, 0.02, 1500)
    node = np.zeros(1500)
    node[4:] = 1.1 * src[:-4]                         # перенос с лагом 4
    node += r.normal(0, 0.001, 1500)
    rec = SEN.calibrate_pair_sensitivity(src, node, lag=4)
    assert rec["pinned"] is True
    assert abs(rec["beta_pinned"] - 1.1) < 0.05
