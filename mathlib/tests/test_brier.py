# -*- coding: utf-8 -*-
"""Тесты Brier и калибровки по корзинам (П7, §7, §10.9)."""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import brier as br  # noqa: E402


def test_brier_perfect_and_worst():
    assert br.brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0
    assert br.brier_score([0.0, 1.0], [1, 0]) == 1.0


def test_brier_known_value():
    # (0.7-1)^2 + (0.3-0)^2 = 0.09+0.09 = 0.18 ; /2 = 0.09
    assert br.brier_score([0.7, 0.3], [1, 0]) == pytest.approx(0.09)


def test_brier_validation():
    with pytest.raises(ValueError):
        br.brier_score([], [])
    with pytest.raises(ValueError):
        br.brier_score([1.2], [1])
    with pytest.raises(ValueError):
        br.brier_score([0.5], [2])
    with pytest.raises(ValueError):
        br.brier_score([0.5, 0.5], [1])


def test_calibration_table_buckets():
    probs = [0.05, 0.15, 0.95, 0.92]
    outs = [0, 0, 1, 1]
    table = br.calibration_table(probs, outs, n_bins=10)
    assert len(table) == 10
    assert table[0]["n"] == 1 and table[1]["n"] == 1
    assert table[9]["n"] == 2
    # верхняя корзина: предсказано ~0.935, наблюдено 1.0
    assert table[9]["obs_freq"] == 1.0
    assert table[9]["mean_pred"] == pytest.approx(0.935)
    # пустые корзины — None
    assert table[5]["n"] == 0 and table[5]["gap"] is None


def test_prob_exactly_one_goes_to_last_bin():
    table = br.calibration_table([1.0], [1], n_bins=10)
    assert table[9]["n"] == 1


def test_perfectly_calibrated_band_zero():
    # 10 прогнозов по 0.7: ровно 7 сбылось → калибровка идеальна в этой корзине
    probs = [0.7] * 10
    outs = [1, 1, 1, 1, 1, 1, 1, 0, 0, 0]
    band = br.calibration_band_pp(probs, outs, n_bins=10)
    assert band == pytest.approx(0.0, abs=1e-9)
    assert br.reliability(probs, outs, n_bins=10) == pytest.approx(0.0, abs=1e-9)


def test_calibration_band_detects_miscalibration():
    # говорим 0.9, сбывается 0.5 → разрыв 40 п.п.
    probs = [0.9] * 10
    outs = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    band = br.calibration_band_pp(probs, outs, n_bins=10)
    assert band == pytest.approx(40.0)


def test_band_none_when_no_bin_reaches_min_n():
    # F2#20: одна точка (n=1 < MIN_BIN_N) НЕ даёт измеримого разрыва → None, а не ложные ~95 п.п.
    assert br.calibration_band_pp([0.5], [1], n_bins=10) is None
    # даже несколько точек, но все в разных корзинах по <5 — None (нечего мерить честно)
    assert br.calibration_band_pp([0.05, 0.25, 0.55, 0.95], [0, 1, 0, 1], n_bins=10) is None


def test_band_counts_bin_with_sufficient_n():
    # корзина с n ≥ MIN_BIN_N учитывается; band — число
    probs = [0.9] * br.MIN_BIN_N
    outs = [0] * br.MIN_BIN_N                       # говорим 0.9, сбылось 0 → разрыв 90 п.п.
    assert br.calibration_band_pp(probs, outs, n_bins=10) == pytest.approx(90.0)


def test_single_extreme_bin_does_not_fabricate_kill():
    # F2#20 ядро: 6 хорошо откалиброванных (0.5, 3/6 сбылось) + 1 одиночная экстремальная точка в др.
    # корзине НЕ должны давать ложный разрыв ~90 п.п. (одиночка отфильтрована по n<MIN_BIN_N).
    probs = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.95]
    outs = [1, 0, 1, 0, 1, 0, 0]
    band = br.calibration_band_pp(probs, outs, n_bins=10)
    assert band is not None and band < 15.0          # одиночка не поднимает band до KILL
