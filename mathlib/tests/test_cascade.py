# -*- coding: utf-8 -*-
"""Тесты движка каскадной амплитуды (mathlib/cascade.py, Этап 2).

Синтетика с известными бетой/лагом → проверяем, что код их восстанавливает; плюс честные
гейты §9/П16: мало истории → не запечатываем; перенос не установлен → не запечатываем.
"""
import math

import numpy as np

from mathlib import cascade as CAS


def _rng():
    return np.random.default_rng(0)


def test_recovers_known_beta():
    r = _rng()
    src = r.normal(0, 0.02, 400)
    node = 1.5 * src + r.normal(0, 0.002, 400)   # бета 1.5, сильная связь
    s = CAS.node_sensitivity(src, node, lag=0)
    assert s is not None
    assert abs(s["beta"] - 1.5) < 0.05
    assert s["r2"] > 0.9
    assert s["перенос_установлен"] is True


def test_recovers_lag():
    r = _rng()
    src = r.normal(0, 0.02, 400)
    node = np.zeros(400)
    node[3:] = 2.0 * src[:-3]                     # узел реагирует с лагом 3 дня
    node += r.normal(0, 0.001, 400)
    s3 = CAS.node_sensitivity(src, node, lag=3)
    s0 = CAS.node_sensitivity(src, node, lag=0)
    assert abs(s3["beta"] - 2.0) < 0.1 and s3["r2"] > 0.9
    assert s0["r2"] < s3["r2"]                    # без правильного лага связь слабее


def test_amplitude_is_beta_times_shock():
    assert abs(CAS.node_amplitude(1.5, -0.05) - (-0.075)) < 1e-9


def test_probability_baseline_and_drift():
    # amplitude=0, threshold=0 → ровно 0.5 (обобщение gaussian_baseline)
    p0 = CAS.node_probability(0.0, resid_std=0.02, horizon_days=5, threshold=0.0)
    assert abs(p0 - 0.5) < 1e-6
    # положительный снос двигает вероятность роста вверх; порог выше нуля — вниз
    assert CAS.node_probability(0.05, 0.02, 5, 0.0) > 0.5
    assert CAS.node_probability(0.0, 0.02, 5, 0.03) < 0.5


def test_node_cascade_sealable_true():
    r = _rng()
    src = r.normal(0, 0.02, 400)
    node = 1.2 * src + r.normal(0, 0.003, 400)
    res = CAS.node_cascade(src, node, shock=-0.05, horizon_days=5, lag=0)
    assert res["sealable"] is True
    assert abs(res["amplitude"] - (1.2 * -0.05)) < 0.01
    assert res["probability"] is not None
    assert res["reliability_r2"] > 0.9


def test_no_data_no_seal():
    r = _rng()
    src = r.normal(0, 0.02, 20)        # мало (< MIN_OBS)
    node = 1.5 * src
    res = CAS.node_cascade(src, node, shock=-0.05, horizon_days=5)
    assert res["sealable"] is False
    assert "нет данных" in res["причина"]
    assert res["sensitivity"] is None


def test_no_transmission_no_seal():
    r = _rng()
    src = r.normal(0, 0.02, 400)
    node = r.normal(0, 0.02, 400)      # независимый шум — переноса нет
    res = CAS.node_cascade(src, node, shock=-0.05, horizon_days=5, lag=0)
    assert res["sealable"] is False
    assert "не установлен" in res["причина"]


def test_ols_beta_degenerate():
    assert CAS.ols_beta([1, 1, 1, 1], [1, 2, 3, 4]) is None   # нулевая дисперсия x
    assert CAS.ols_beta([1], [1]) is None


def test_lag_for_pair_directed_semantics():
    # Ночная смена 04.07: лаг A→B больше не применяется слепо к B→A.
    from mathlib.cascade import _lag_for_pair
    links = [{"pair": ["OIL", "AIR"], "lag_days": 3, "directed": True},
             {"pair": ["CU", "COPX"], "lag_days": 2}]           # ненаправленная
    assert _lag_for_pair("OIL", "AIR", links) == 3              # точное направление
    assert _lag_for_pair("AIR", "OIL", links) == 0              # обратное к directed — НЕ наш лаг
    assert _lag_for_pair("CU", "COPX", links) == 2              # undirected симметричен
    assert _lag_for_pair("COPX", "CU", links) == 2
    assert _lag_for_pair("X", "Y", links) == 0


def test_unpriced_realized_none_is_none_not_crash():
    # M11 (ревью 04.07): realized_move=None (короткая история) → честный None, не TypeError.
    from mathlib.cascade import cascade_edge
    assert cascade_edge(0.05, None) is None
    r = cascade_edge(0.05, 0.01)
    assert r is not None and r["edge"] == 0.04


# ── Этап1 (аудит 07.2026): гард вырожденной подгонки / короткого ряда ──────────────────
def test_short_series_returns_none_no_exception():
    """Ряд короче MIN_TRANSFER_OBS → node_sensitivity честно None, БЕЗ исключения (2-3 точки)."""
    for k in (2, 3, 10, CAS.MIN_TRANSFER_OBS - 1):
        assert CAS.node_sensitivity([0.01] * k, [0.02] * k, min_obs=2) is None


def test_degenerate_fit_not_established():
    """Идеальный фит (node = 2·source, n≥порога) → r²≈1.0, но перенос НЕ «установлен» (вырожден)."""
    src = list(np.random.default_rng(1).normal(0, 0.02, 40))
    node = [2.0 * x for x in src]                    # r²=1.0 ровно
    out = CAS.node_sensitivity(src, node, min_obs=20)
    assert out is not None
    assert out["r2"] >= CAS.R2_DEGENERATE
    assert out["degenerate"] is True
    assert out["перенос_установлен"] is False        # вырожденный фит не течёт в ярус A


def test_genuine_transfer_still_established():
    """Реальный (не вырожденный) перенос с шумом и достаточной историей — перенос установлен."""
    r = np.random.default_rng(2)
    src = r.normal(0, 0.02, 300)
    node = 1.3 * src + r.normal(0, 0.01, 300)        # сильная, но НЕ идеальная связь
    out = CAS.node_sensitivity(list(src), list(node), min_obs=60)
    assert out is not None and out["degenerate"] is False
    assert out["перенос_установлен"] is True
