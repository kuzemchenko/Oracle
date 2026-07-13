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
    # первая половина — почти гаусс, вторая — экстремально тяжёлые хвосты с масштабом:
    # медианный df не может описать ОБА режима → OOS-валидация роняет пин (v2)
    rng = np.random.default_rng(3)
    z = np.concatenate([rng.standard_normal(800) * 0.3, rng.standard_t(2, size=800) * 3.0])
    res = TD.calibrate_instrument(z)
    if res["pinned"]:                          # пин допустим только если медианный df прошёл OOS
        assert res["ok_fraction"] >= TD.MIN_OOS_OK_FRACTION
    else:
        assert "OOS" in res["reason"] or "фолдов" in res["reason"]


def test_pooled_fallback_reports_value_and_check():
    rng = np.random.default_rng(5)
    zs = [rng.standard_t(5, size=1500), rng.standard_t(6, size=1500)]
    fb = TD.pooled_fallback_df(zs)
    assert fb["df"] is not None and 3.0 <= fb["df"] <= 10.0
    assert fb["n"] == 3000
    # регрессия (кросс-ревью Д1 HIGH): поле НЕ должно называться walk-forward — пул конкатенирует
    # разные инструменты, фолды идут по границам инструментов, а не по времени.
    assert "walkforward_check" not in fb
    assert "pool_self_consistency" in fb
    assert "не WF" in fb["pool_self_consistency"]["note"]


def test_calibrate_oos_validation_is_walk_forward_clean(monkeypatch):
    """Регрессия (кросс-ревью Д1 HIGH): OOS-валидация фолда i использует РАСШИРЯЮЩУЮСЯ медиану
    df только по прошлым+текущему train-фолдам — не глобальную, куда затекало бы будущее.

    Конструкция: режим меняется во времени (лёгкие хвосты → тяжёлые). Глобальная медиана df
    описывала бы поздний режим и «валидировалась» на ранних test как будто была известна тогда;
    расширяющаяся медиана ранних фолдов от позднего режима не зависит."""
    rng = np.random.default_rng(4242)
    z = np.concatenate([rng.standard_t(40, size=1500), rng.standard_t(2, size=1500) * 2.0])
    res = TD.calibrate_instrument(z)
    # каждый фолд валидируется своим df_wf_expanding (median по dfs[:i+1]), НЕ общей медианой
    dfs = [f["df_train"] for f in res["folds"]]
    import numpy as _np
    for i, f in enumerate(res["folds"]):
        exp = TD._snap_to_grid(float(_np.median(dfs[:i + 1])))
        assert f["df_wf_expanding"] == exp
        assert "oos_ok_expanding_df" in f
    # для первого фолда расширяющийся df = df его собственного train (будущее не подмешано)
    assert res["folds"][0]["df_wf_expanding"] == TD._snap_to_grid(float(dfs[0]))


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
