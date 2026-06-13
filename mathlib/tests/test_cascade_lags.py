# -*- coding: utf-8 -*-
"""Тесты калибровки ценового lead-lag рёбер цепочек (mathlib/calibration/cascade_lags.py)."""
import numpy as np

from mathlib.calibration import cascade_lags as CL
from mathlib.calibration import loader as L


def _series(symbol, closes):
    s = L.Series.__new__(L.Series)
    s.symbol = symbol
    n = len(closes)
    s.dates = np.array([f"2020-01-{i+1:02d}" for i in range(n)]) if n < 28 else \
        np.array([f"d{i}" for i in range(n)])
    s.close = np.asarray(closes, dtype=float)
    s.adj = s.close.copy()
    s.open = s.close.copy(); s.high = s.close.copy(); s.low = s.close.copy()
    s.volume = np.ones(n)
    return s


def test_pvalue_monotone_and_bounds():
    assert CL._pvalue(0.0, 200) > 0.5            # нулевая корреляция → большой p
    assert CL._pvalue(0.9, 200) < 0.001          # сильная корреляция → крошечный p
    assert CL._pvalue(0.5, 3) is None            # мало точек → None (П8)


def test_weekly_downsamples_by_five():
    s = _series("AAA", list(range(100)))
    w = CL._weekly(s)
    assert len(w) == 20                          # каждый 5-й
    assert w.close[1] == 5.0


def test_calibrate_chain_recovers_known_lag(monkeypatch):
    # y = x, сдвинутый на 3 дня вперёд → measure_pair должен найти ненулевой лаг и связь
    rng = np.random.default_rng(0)
    base = 100 + np.cumsum(rng.normal(0, 1, 400))
    x = _series("XXX.US", base)
    y = _series("YYY.US", np.concatenate([base[3:], base[:3]]))  # сдвиг

    def fake_aligned(symbols, db=None):
        m = {"XXX.US": x, "YYY.US": y}
        return x.dates, {s: m[s] for s in symbols}
    monkeypatch.setattr(CL.L, "load_aligned", fake_aligned)

    chain = {"id": "t", "nodes": [
        {"order": 1, "instruments": ["XXX.US"]},
        {"order": 2, "instruments": ["YYY.US"]}],
        "edges": [{"from": 1, "to": 2, "lag_days": 45}]}
    res = CL.calibrate_chain(chain, db=":memory:")
    e = res["edges"][0]
    assert e["lag_hypothesis_days"] == 45        # гипотеза экон. лага СОХРАНЕНА (не затёрта)
    assert e["price_leadlag"] is not None
    assert abs(e["price_leadlag"]["best_r"]) > 0.3   # связь обнаружена
    assert "honesty_note" in res                 # явная пометка про экон. vs ценовой лаг


def test_insufficient_history_marked():
    short = _series("S.US", list(range(40)))
    def fake_aligned(symbols, db=None):
        return short.dates, {s: short for s in symbols}
    import types
    CL2 = CL
    orig = CL.L.load_aligned
    try:
        CL.L.load_aligned = fake_aligned
        chain = {"id": "t", "nodes": [
            {"order": 1, "instruments": ["A.US"]}, {"order": 2, "instruments": ["B.US"]}],
            "edges": [{"from": 1, "to": 2, "lag_days": 30}]}
        res = CL.calibrate_chain(chain, db=":memory:")
        assert res["edges"][0]["price_leadlag"] is None   # мало истории → нет данных
    finally:
        CL.L.load_aligned = orig
