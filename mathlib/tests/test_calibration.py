# -*- coding: utf-8 -*-
"""Тесты детерминированной калибровки §23.1 (честная зона walk-forward).

Проверяется ЛОГИКА на синтетике с известным ответом — не подгонка под боевые данные.
"""
import sys
import pathlib

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib.calibration import (  # noqa: E402
    loader, walkforward as wf, backgrounds as bg, costs, timing,
    manipulation as mp, causal, precursors as pc, dataquality as dq,
)


def make_series(symbol, close, volume=None, high=None, low=None, open_=None):
    """Синтетический loader.Series из массива close (даты — фиктивные ISO)."""
    close = np.asarray(close, float)
    n = close.size
    volume = np.full(n, 1e6) if volume is None else np.asarray(volume, float)
    high = close * 1.01 if high is None else np.asarray(high, float)
    low = close * 0.99 if low is None else np.asarray(low, float)
    open_ = close if open_ is None else np.asarray(open_, float)
    rows = [(f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}", open_[i], high[i], low[i],
             close[i], close[i], volume[i]) for i in range(n)]
    return loader.Series(symbol, rows)


# ---------- walk_forward ----------

def test_walk_forward_sliding_counts_and_nonoverlap():
    folds = wf.walk_forward(100, train_size=40, test_size=20)
    assert len(folds) == 3                       # tests at [40:60],[60:80],[80:100]
    for f in folds:
        assert f.train_end == f.test_start       # train идёт строго перед test
        assert f.test_end - f.test_start == 20
        assert f.train_end - f.train_start == 40  # скользящее окно фикс. длины
    # непересечение последовательных test-окон
    assert folds[0].test_end == folds[1].test_start


def test_walk_forward_anchored_grows_train():
    folds = wf.walk_forward(100, train_size=40, test_size=20, anchored=True)
    assert all(f.train_start == 0 for f in folds)
    assert folds[1].train_end > folds[0].train_end


def test_walk_forward_too_short_raises():
    with pytest.raises(ValueError):
        wf.walk_forward(30, train_size=40, test_size=20)


# ---------- backgrounds ----------

def test_background_recovers_std():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.02, 5000)
    b = bg.background(x)
    assert not b["insufficient"]
    assert abs(b["std"] - 0.02) < 0.002
    assert b["q99"] > b["q95"] > 0


def test_background_insufficient():
    assert bg.background([1, 2, 3])["insufficient"] is True


def test_empirical_p_two_sided_extremes():
    rng = np.random.default_rng(1)
    base = rng.normal(0, 1, 2000)
    p_extreme = bg.empirical_p_two_sided(8.0, base)   # далёкий хвост → крошечный p
    p_center = bg.empirical_p_two_sided(0.0, base)     # центр → большой p
    assert p_extreme < 0.01 < p_center


def test_metric_series_dvol_finite():
    s = make_series("X", np.linspace(100, 120, 60), volume=np.linspace(1e6, 2e6, 60))
    d = bg.metric_series(s, "dvol")
    assert d.size > 0 and np.all(np.isfinite(d))


# ---------- costs ----------

def test_half_spread_tiers_monotone():
    assert costs.half_spread_bps(1e9) < costs.half_spread_bps(50e6) < costs.half_spread_bps(1e6)


def test_slippage_scales_linearly():
    adv = 1e8
    s1 = costs.slippage_bps(1e5, adv)
    s2 = costs.slippage_bps(2e5, adv)
    assert s2 == pytest.approx(2 * s1)


def test_instrument_costs_structure_and_short_marked():
    s = make_series("LIQ", np.full(80, 100.0), volume=np.full(80, 1e7))
    c = costs.instrument_costs(s, order_usd=500)
    assert c["round_trip_bps"] > 0
    assert c["short_borrow_fee_bps"] is None          # П8: нет данных, не выдумываем
    assert "нет данных" in c["short_borrow_provenance"]


# ---------- timing ----------

def test_death_threshold_finds_crossover():
    # малый ход → продолжение +; большой ход (>=2.0) → продолжение −
    # (малый сильнее по модулю, чтобы объединённое среднее на низком пороге было > 0)
    mag = np.concatenate([np.full(100, 1.0), np.full(100, 2.5)])
    cont = np.concatenate([np.full(100, 0.02), np.full(100, -0.01)])
    grid = np.arange(0.5, 3.01, 0.5)
    thr, table = timing.death_threshold(mag, cont, grid, min_count=20)
    assert thr is not None and 1.5 <= thr <= 2.5
    assert len(table) == len(grid)


def test_death_threshold_none_when_always_positive():
    mag = np.linspace(0.5, 3.0, 200)
    cont = np.full(200, 0.02)
    thr, _ = timing.death_threshold(mag, cont, np.arange(0.5, 3.01, 0.5), min_count=20)
    assert thr is None


def test_build_events_shapes():
    rng = np.random.default_rng(2)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    s = make_series("RW", close, volume=rng.uniform(1e6, 2e6, 400))
    ev = timing.build_events(s, k=20, h=10, vol_window=60)
    assert ev["spent_sigma"].size == ev["cont"].size > 100
    assert np.all(np.isfinite(ev["spent_sigma"]))


# ---------- manipulation ----------

def test_false_breakout_detects_trap():
    # явная ловушка: пробой нового максимума, затем возврат вниз и спад
    base = list(np.full(25, 100.0))
    trap = [101.0, 102.0]               # пробой максимума (=100)
    after = list(np.linspace(99.0, 90.0, 25))   # возврат ниже уровня и падение
    close = np.array(base + trap + after)
    s = make_series("TRAP", close, high=close * 1.001, low=close * 0.999)
    be = mp.build_breakout_events(s, lookback=20, max_revert=5, h=20)
    assert be["idx"].size >= 1
    # хотя бы один пробой реверсировал (revert>=1)
    assert np.any(be["revert"] >= 1)


def test_stop_hunt_detects_rejection():
    # пробой опоры вниз с закрытием обратно выше неё
    base = list(np.full(25, 100.0))
    close = np.array(base + [100.5] + list(np.linspace(101, 108, 25)))
    low = close.copy()
    low[25] = 98.0                       # прокол минимума интрадей
    high = close * 1.001
    s = make_series("SH", close, high=high, low=low)
    se = mp.build_stophunt_events(s, lookback=20, h=20, atr_window=14)
    assert se["idx"].size >= 1
    assert np.all(se["pen_atr"] > 0)


# ---------- causal ----------

def test_lead_lag_recovers_injected_lag():
    rng = np.random.default_rng(3)
    rx = rng.normal(0, 1, 3000)
    L = 3
    ry = np.empty_like(rx)
    ry[:L] = rng.normal(0, 1, L)
    ry[L:] = rx[:-L]                     # y запаздывает за x на L → x опережает y на +L
    _, best = causal.lead_lag(rx, ry, max_lag=10)
    assert best["lag"] == L


def test_measure_pair_insufficient_on_short():
    a = make_series("A", np.linspace(100, 110, 20))
    b = make_series("B", np.linspace(50, 55, 20))
    assert causal.measure_pair(a, b)["insufficient"] is True


# ---------- precursors ----------

def _flat_series(symbol, level=100.0, n=30, spike_at=None, spike_ret=None):
    c = np.full(n, level, dtype=float)
    if spike_at is not None:
        c[spike_at] = level * (1.0 + spike_ret)   # выброс на день spike_at, день spike_at+1 возвращается к level
    rows = [(f"2020-01-{i + 1:02d}", c[i], c[i] * 1.001, c[i] * 0.999, c[i], c[i], 1e6)
            for i in range(n)]
    return loader.Series(symbol, rows)


def test_detect_bad_tick_idiosyncratic():
    # У X выброс −30% с откатом, пиры спокойны → битый тик
    x = _flat_series("X", spike_at=10, spike_ret=-0.30)
    p1 = _flat_series("P1")
    p2 = _flat_series("P2")
    aligned = {"X": x, "P1": p1, "P2": p2}
    res = dq.detect_bad_ticks(aligned, {"X": ["P1", "P2"], "P1": [], "P2": []})
    assert len(res["X"]) == 1
    assert res["X"][0]["date"] == "2020-01-11"        # индекс 10 → 11-е


def test_bad_tick_not_flagged_when_peers_move():
    # Тот же выброс, но пиры тоже падают в этот день → рыночное событие, НЕ битый тик
    x = _flat_series("X", spike_at=10, spike_ret=-0.30)
    p1 = _flat_series("P1", spike_at=10, spike_ret=-0.28)
    aligned = {"X": x, "P1": p1}
    res = dq.detect_bad_ticks(aligned, {"X": ["P1"], "P1": []})
    assert res["X"] == []


def test_bad_tick_needs_reversal():
    # Выброс БЕЗ отката (ступень, как сплит) → не битый тик
    c = [100.0] * 10 + [70.0] * 10            # шаг вниз без возврата
    rows = [(f"2020-01-{i + 1:02d}", c[i], c[i] * 1.001, c[i] * 0.999, c[i], c[i], 1e6) for i in range(20)]
    x = loader.Series("X", rows)
    p1 = _flat_series("P1", n=20)
    res = dq.detect_bad_ticks({"X": x, "P1": p1}, {"X": ["P1"], "P1": []})
    assert res["X"] == []


def test_adjusted_view_removes_split_artifact():
    # raw close удваивается на «сплите» (индекс 5), adjusted_close непрерывен
    close = [10.0] * 5 + [20.0] * 5
    adj = [10.0] * 10
    rows = [(f"2020-01-{i + 1:02d}", close[i], close[i] * 1.01, close[i] * 0.99,
             close[i], adj[i], 1e6) for i in range(10)]
    s = loader.Series("SPLIT", rows)
    av = loader.adjusted_view(s)
    assert np.allclose(av.close, adj)                       # close переведён на adjusted
    assert (close[5] / close[4] - 1) > 0.9                  # raw-доходность на сплите огромна
    assert abs(av.close[5] / av.close[4] - 1) < 1e-9        # adjusted-доходность ≈ 0
    assert np.all(av.high >= av.close * 0.999)              # OHLC остались согласованными
    # biggest_moves на adjusted не считает сплит-день большим движением
    moves, _excluded = pc.biggest_moves(av, window=1, top_n=1, min_gap=1)
    assert abs(moves[0]["magnitude_pct"]) < 1.0


def test_biggest_moves_finds_largest_and_dedups():
    close = np.full(200, 100.0)
    close[100:120] = np.linspace(100, 160, 20)    # крупный рост
    close[120:] = 160.0
    s = make_series("M", close)
    moves, _excluded = pc.biggest_moves(s, window=20, top_n=3, min_gap=20)
    assert moves[0]["direction"] == "up"
    assert moves[0]["magnitude_pct"] > 30
    idxs = [m["end_idx"] for m in moves]          # дедуп: события не ближе min_gap
    assert all(abs(a - b) >= 20 for i, a in enumerate(idxs) for b in idxs[i + 1:])
