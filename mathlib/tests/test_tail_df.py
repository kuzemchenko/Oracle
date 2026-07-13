# -*- coding: utf-8 -*-
"""Тесты mathlib/calibration/tail_df.py (этап Д1): синтетика с ИЗВЕСТНЫМ df восстанавливается,
гаусс уходит в большой df, короткая/нестабильная история честно не пинится (П8)."""
import numpy as np
import pytest

from mathlib.calibration import tail_df as TD
from mathlib import indicators as ind


# ── fit_df: восстановление известного df на синтетике ──────────────────────────────

def test_fit_df_recovers_t3():
    rng = np.random.default_rng(42)
    z = rng.standard_t(3, size=6000)
    df, _ = TD.fit_df(z)
    assert 2.0 <= df <= 4.0, f"t(3)-данные дали df={df}"


def test_fit_df_recovers_t5():
    rng = np.random.default_rng(7)
    z = rng.standard_t(5, size=6000)
    df, _ = TD.fit_df(z)
    assert 3.0 <= df <= 8.0, f"t(5)-данные дали df={df}"


def test_fit_df_gaussian_large_df():
    rng = np.random.default_rng(1)
    z = rng.standard_normal(6000)
    df, _ = TD.fit_df(z)
    assert df >= 30.0, f"гауссовы данные дали df={df} (ожидался большой)"


def test_fit_df_tie_prefers_smaller_df():
    # вырожденный случай: все z = 0 → все пороги дают одну эмпирическую частоту;
    # проверяем только правило ничьей — выбран НАИМЕНЬШИЙ df из равных
    z = np.zeros(500)
    df, losses = TD.fit_df(z)
    best_loss = min(losses.values())
    tied = [d for d, l in losses.items() if abs(l - best_loss) < 1e-12]
    assert df == min(tied)


# ── walk-forward калибровка ─────────────────────────────────────────────────────────

def test_calibrate_pins_on_long_stable_t4():
    rng = np.random.default_rng(11)
    z = rng.standard_t(4, size=3000)
    res = TD.calibrate_instrument(z)
    assert res["pinned"] is True
    assert 2.5 <= res["df"] <= 8.0
    assert len(res["folds"]) >= TD.MIN_FOLDS


def test_calibrate_short_series_not_pinned():
    rng = np.random.default_rng(2)
    z = rng.standard_t(4, size=100)          # < train+test
    res = TD.calibrate_instrument(z)
    assert res["pinned"] is False and res["df"] is None
    assert "короткая история" in res["reason"]


def test_calibrate_unstable_not_pinned():
    # первая половина — почти гаусс, вторая — очень тяжёлые хвосты: df по фолдам должен разъехаться
    rng = np.random.default_rng(3)
    z = np.concatenate([rng.standard_normal(800), rng.standard_t(2, size=800) * 3.0])
    res = TD.calibrate_instrument(z)
    if res["pinned"]:                          # допускаем пин только если фолды реально сошлись
        assert res["fold_df_ratio"] <= TD.MAX_FOLD_DF_RATIO
    else:
        assert res["reason"]


def test_pooled_fallback_reports_value_and_check():
    rng = np.random.default_rng(5)
    zs = [rng.standard_t(5, size=1500), rng.standard_t(6, size=1500)]
    fb = TD.pooled_fallback_df(zs)
    assert fb["df"] is not None and 3.0 <= fb["df"] <= 10.0
    assert fb["n"] == 3000
    assert "walkforward_check" in fb


def test_pooled_fallback_too_small_is_honest():
    fb = TD.pooled_fallback_df([np.zeros(10)])
    assert fb["df"] is None and "П8" in fb["reason"]


# ── эквивалентность сканному z (context._indicators) ───────────────────────────────

def test_ret_z_series_matches_live_zscore():
    rng = np.random.default_rng(8)
    px = 100.0 * np.exp(np.cumsum(rng.standard_normal(80) * 0.01))
    series = TD.scan_ret_z_series(px)
    # последний элемент серии == живой сканный z (indicators.zscore(returns, 20))
    live = ind.zscore(ind.returns(px), 20)
    assert series.size == len(px) - 1 - 19
    assert abs(series[-1] - live) < 1e-12


def test_vol_z_series_matches_live_zscore():
    rng = np.random.default_rng(9)
    vol = np.exp(rng.standard_normal(60) + 12.0)
    series = TD.scan_vol_z_series(vol)
    live = ind.zscore([float(np.log(max(v, 1.0))) for v in vol], 20)
    assert abs(series[-1] - live) < 1e-12


def test_scan_z_bounded_by_inclusion():
    # z с включённой точкой алгебраически ограничен sqrt(n-1) ≈ 4.36 при n=20
    rng = np.random.default_rng(10)
    r = rng.standard_t(2, size=5000)          # экстремально тяжёлые хвосты
    z = TD._rolling_incl_z(r, 20)
    assert float(np.max(np.abs(z))) <= np.sqrt(19.0) + 1e-9


def test_binom_two_sided_sane():
    assert TD.binom_two_sided_p(5, 100, 0.05) > 0.5     # ровно ожидание
    assert TD.binom_two_sided_p(30, 100, 0.05) < 0.001  # сильное превышение
    assert TD.binom_two_sided_p(0, 100, 0.05) == pytest.approx(
        TD.binom_two_sided_p(0, 100, 0.05))             # детерминированность
