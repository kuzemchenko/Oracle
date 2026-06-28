# -*- coding: utf-8 -*-
"""Тесты форвард-промоушена каскадных рёбер (mathlib/calibration/forward_promotion.py).

Решение владельца 28.06: ребро → ярус A по N≥30 запечатанным форвард-исходам + значимый скилл (§10).
"""
from mathlib.calibration import forward_promotion as FP


def _confident(n, hit_frac, p_conf=0.8):
    """n исходов: доля hit_frac «попаданий». Уверенный прогноз p_conf в направлении выше порога.
    Попадание: outcome=1 (для p≥0.5). Промах: outcome=0."""
    probs, outs = [], []
    n_hit = round(n * hit_frac)
    for i in range(n):
        probs.append(p_conf)
        outs.append(1 if i < n_hit else 0)
    return probs, outs


def test_binom_sf_basic():
    assert FP._binom_sf_ge(0, 10) == 1.0
    assert FP._binom_sf_ge(11, 10) == 0.0
    # P(X≥5 | n=10,p=.5) > 0.5 (включает медиану)
    assert FP._binom_sf_ge(5, 10) > 0.5
    # сильный перекос значим
    assert FP._binom_sf_ge(28, 30) < 0.001


def test_strong_skill_promotes():
    probs, outs = _confident(30, 0.8)              # 24/30 попаданий
    rec = FP.promote_decision(probs, outs, beta_fullsample=1.2)
    assert rec["promote"] is True
    assert rec["n"] == 30 and rec["enough_outcomes"] is True
    assert rec["skill_significant"] is True
    assert 0 < rec["reliability"] <= FP.FORWARD_RELIABILITY_CAP
    assert rec["beta_fullsample"] == 1.2
    assert "ПРОМОУШЕН" in rec["причина"]


def test_below_min_outcomes_blocks_even_perfect():
    probs, outs = _confident(20, 1.0)              # идеальный скилл, но N<30
    rec = FP.promote_decision(probs, outs)
    assert rec["promote"] is False
    assert rec["enough_outcomes"] is False
    assert rec["reliability"] == 0.0
    assert "20<30" in rec["причина"]


def test_coinflip_not_significant():
    probs, outs = _confident(40, 0.5)              # 50% попаданий — не лучше монетки
    rec = FP.promote_decision(probs, outs)
    assert rec["promote"] is False
    assert rec["skill_significant"] is False


def test_reliability_capped():
    assert FP.reliability_from_skill(1.0) == FP.FORWARD_RELIABILITY_CAP
    assert FP.reliability_from_skill(0.5) == 0.0
    assert FP.reliability_from_skill(None) == 0.0
    assert FP.reliability_from_skill(0.6) == round(min(0.2, FP.FORWARD_RELIABILITY_CAP), 4)


def test_brier_gate_blocks_unconfident_but_directionally_right():
    # 30 исходов, направление почти всегда верное, НО вероятность на грани 0.5 → Brier≈0.25, не ниже
    probs = [0.5] * 30
    outs = [1] * 21 + [0] * 9                       # hit-rate 70% (значимо), но p=0.5 → Brier=0.25
    rec = FP.promote_decision(probs, outs)
    # Brier ровно 0.25 (не строго ниже) → скилл незначим по гейту Brier
    assert rec["brier"] == 0.25
    assert rec["promote"] is False


def test_aggregate_and_promote_all():
    rows = []
    # ребро A: 30 уверенных попаданий → промоушен
    pa, oa = _confident(30, 0.8)
    rows += [{"edge_key": "VRT.US->GEV.US", "probability": p, "outcome": o,
              "beta_fullsample": 1.1} for p, o in zip(pa, oa)]
    # ребро B: 10 исходов → мало
    pb, ob = _confident(10, 1.0)
    rows += [{"edge_key": "X->Y", "probability": p, "outcome": o} for p, o in zip(pb, ob)]
    # мусор: без вероятности/исхода — отбрасывается (П8)
    rows.append({"edge_key": "X->Y", "probability": None, "outcome": 1})
    res = FP.promote_all(rows)
    assert res["VRT.US->GEV.US"]["promote"] is True
    assert res["VRT.US->GEV.US"]["beta_fullsample"] == 1.1
    assert res["X->Y"]["promote"] is False
    assert res["X->Y"]["n"] == 10                   # мусорная строка не учтена
