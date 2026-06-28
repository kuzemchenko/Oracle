# -*- coding: utf-8 -*-
"""Тесты §3c: ярусы звена A/B/C, многозвенная свёртка с пробросом дисперсии, edge.

Свёртку считает КОД (инвариант 6). Проверяем: амплитуда = shock₀×Πgain; дисперсия растёт с
длиной/шаткостью цепи; seal только при всех A; edge=амплитуда−отыгранное лечит схлопывание в 1-й
порядок; «нет данных» в звене → путь не разрешим (П8).
"""
import math

import numpy as np

from mathlib import cascade as CAS


def _rng():
    return np.random.default_rng(0)


# ── ярусы звена ──────────────────────────────────────────────────────────────────
def test_link_empirical_tier_a():
    r = _rng()
    src = r.normal(0, 0.02, 400)
    node = 1.3 * src + r.normal(0, 0.002, 400)
    lk = CAS.link_empirical(src, node, lag=0)
    assert lk["tier"] == "A"
    assert abs(lk["gain"] - 1.3) < 0.05
    assert lk["established"] is True
    assert lk["reliability"] > 0.9            # reliability = R² при установленном переносе
    assert lk["gain_sd"] is not None and lk["gain_sd"] > 0


def test_link_empirical_no_data_is_none():
    r = _rng()
    src = r.normal(0, 0.02, 20)               # < MIN_OBS
    assert CAS.link_empirical(src, 1.5 * src, lag=0) is None


def test_link_empirical_no_transmission_zero_reliability():
    r = _rng()
    src = r.normal(0, 0.02, 400)
    node = r.normal(0, 0.02, 400)             # независимый шум
    lk = CAS.link_empirical(src, node, lag=0)
    assert lk["established"] is False
    assert lk["reliability"] == 0.0           # перенос не установлен → надёжность 0


def test_link_structural_tier_b_caps_reliability():
    lk = CAS.link_structural(0.4, 2.0, lag=5)       # 40% выручки × рычаг 2.0
    assert lk["tier"] == "B"
    assert abs(lk["gain"] - 0.8) < 1e-9
    assert lk["reliability"] <= CAS.STRUCTURAL_RELIABILITY_CAP
    # явная завышенная надёжность зажимается потолком
    assert CAS.link_structural(0.4, 2.0, reliability=0.99)["reliability"] == CAS.STRUCTURAL_RELIABILITY_CAP


def test_link_mechanism_tier_c_low_prior_wide_band():
    lk = CAS.link_mechanism(1.0)
    assert lk["tier"] == "C"
    assert lk["reliability"] <= CAS.MECHANISM_RELIABILITY_CAP
    assert lk["gain_sd"] >= abs(lk["gain"])         # широкая полоса по умолчанию (CV≈1)
    assert lk["established"] is False


# ── свёртка цепочки ──────────────────────────────────────────────────────────────
def test_compose_amplitude_is_shock_times_product():
    a = {"tier": "A", "gain": 0.5, "gain_sd": 0.0, "reliability": 0.8, "lag": 0, "established": True}
    b = {"tier": "B", "gain": 2.0, "gain_sd": 0.0, "reliability": 0.6, "lag": 5, "established": None}
    out = CAS.compose_chain([a, b], shock0=0.1)
    assert abs(out["amplitude"] - 0.1) < 1e-9       # 0.1 × 0.5 × 2.0
    assert abs(out["reliability"] - 0.48) < 1e-9    # 0.8 × 0.6
    assert out["lag_total"] == 5
    assert out["lowest_tier"] == "B"


def test_compose_variance_propagation_exact():
    # независимые факторы: shock0=0.1 (точно), g1=0.5±0.1, g2=2.0±0.4
    a = {"tier": "A", "gain": 0.5, "gain_sd": 0.1, "reliability": 0.8, "lag": 0, "established": True}
    b = {"tier": "B", "gain": 2.0, "gain_sd": 0.4, "reliability": 0.6, "lag": 0, "established": None}
    out = CAS.compose_chain([a, b], shock0=0.1)
    # Var = Π(μ²+σ²) − (Πμ)² = (0.01)(0.26)(4.16) − 0.01² ; ×нет (shock0 sd=0)
    expected_sd = math.sqrt(0.01 * 0.26 * 4.16 - 0.1 ** 2)
    assert abs(out["amplitude_sd"] - round(expected_sd, 6)) < 1e-6


def test_compose_weak_link_widens_band():
    tight = {"tier": "A", "gain": 1.0, "gain_sd": 0.02, "reliability": 0.9, "lag": 0, "established": True}
    shaky = {"tier": "C", "gain": 1.0, "gain_sd": 0.9, "reliability": 0.15, "lag": 0, "established": False}
    narrow = CAS.compose_chain([tight, tight], shock0=0.1)["amplitude_sd"]
    wide = CAS.compose_chain([tight, shaky], shock0=0.1)["amplitude_sd"]
    assert wide > narrow                            # шаткое звено → шире полоса


def test_compose_sealable_only_when_all_a_established():
    a1 = {"tier": "A", "gain": 1.2, "gain_sd": 0.05, "reliability": 0.9, "lag": 0, "established": True}
    a2 = {"tier": "A", "gain": 0.8, "gain_sd": 0.03, "reliability": 0.85, "lag": 2, "established": True}
    assert CAS.compose_chain([a1, a2], shock0=-0.05)["sealable_path"] is True
    # один структурный B → путь research-only
    b = CAS.link_structural(0.4, 2.0)
    res = CAS.compose_chain([a1, b], shock0=-0.05)
    assert res["sealable_path"] is False
    assert res["lowest_tier"] == "B"
    # один A с неустановленным переносом → тоже не sealable
    a_unest = {"tier": "A", "gain": 1.0, "gain_sd": 0.5, "reliability": 0.0, "lag": 0, "established": False}
    assert CAS.compose_chain([a1, a_unest], shock0=-0.05)["sealable_path"] is False


def test_compose_missing_link_not_resolvable():
    a = {"tier": "A", "gain": 1.0, "gain_sd": 0.05, "reliability": 0.9, "lag": 0, "established": True}
    res = CAS.compose_chain([a, None], shock0=0.1)
    assert res["sealable_path"] is False
    assert res["amplitude"] is None
    assert "без данных" in res["причина"]


# ── edge = амплитуда − отыгранное ────────────────────────────────────────────────
def test_cascade_edge_unpriced_fraction():
    e = CAS.cascade_edge(-0.075, realized_move=-0.06, amplitude_sd=0.02)
    assert abs(e["edge"] - (-0.015)) < 1e-9
    assert abs(e["unpriced_fraction"] - 0.2) < 1e-9   # 20% движения ещё не в цене


def test_cascade_edge_fully_priced_first_order():
    # 1-й порядок, уже отыгран (BNO после −3%): realized ≈ amplitude → edge≈0
    e = CAS.cascade_edge(-0.03, realized_move=-0.03)
    assert abs(e["edge"]) < 1e-9
    assert e["unpriced_fraction"] == 0.0


def test_cascade_edge_overshoot_negative_fraction():
    e = CAS.cascade_edge(-0.03, realized_move=-0.05)   # рынок ушёл дальше расчёта
    assert e["unpriced_fraction"] < 0


def test_cascade_edge_tiny_amplitude_unmeasurable_not_absurd():
    # |амплитуда| ниже шумового порога → доля «отыграно» НЕ определена (П8), а НЕ абсурд 2890%
    # от деления на почти-ноль (баг, который ловил пользователь). Сам edge при этом считается.
    e = CAS.cascade_edge(0.002764, realized_move=0.079893)
    assert e["unpriced_fraction"] is None
    assert e["edge"] == round(0.002764 - 0.079893, 6)


def test_window_return_aligns_horizons():
    import math
    prices = [100, 101, 102, 103, 104, 105]                  # 5 баров (окно) роста
    assert CAS.window_return(prices, window=5) == round(math.log(105 / 100), 6)
    assert CAS.window_return([100, 101], window=5) is None    # мало истории → None (П8)


def test_edge_rank_score():
    assert CAS.edge_rank_score(-0.015, 0.48) == round(0.015 * 0.48, 6)
    assert CAS.edge_rank_score(None, 0.5) == 0.0
