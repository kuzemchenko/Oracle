# -*- coding: utf-8 -*-
"""Тесты форвард-промоушена каскадных рёбер (mathlib/calibration/forward_promotion.py).

Решение владельца 28.06: ребро → ярус A по N≥30 запечатанным форвард-исходам + значимый скилл (§10).
F0#5: скилл меряется НАД БАЗОВОЙ СТАВКОЙ (climatology), не над монеткой 0.5 — base-rate-монетка НЕ промоутится.
"""
from mathlib.calibration import forward_promotion as FP


def _make(n, base_rate, accuracy, p_conf=0.8):
    """n исходов с долей base_rate «вверх»; модель угадывает НАПРАВЛЕНИЕ с долей accuracy при уверенности
    p_conf. Статистики порядко-независимы, поэтому: hit_rate=accuracy, base_rate=доля единиц,
    brier=accuracy·(1−p_conf)²+(1−accuracy)·p_conf² (не зависит от base_rate)."""
    n_up = round(n * base_rate)
    n_correct = round(n * accuracy)
    probs, outs = [], []
    for i in range(n):
        y = 1 if i < n_up else 0
        correct = i < n_correct
        predicted_up = (y == 1) if correct else (y == 0)
        probs.append(p_conf if predicted_up else 1.0 - p_conf)
        outs.append(y)
    return probs, outs


def test_binom_sf_basic():
    assert FP._binom_sf_ge(0, 10) == 1.0
    assert FP._binom_sf_ge(11, 10) == 0.0
    assert FP._binom_sf_ge(5, 10) > 0.5            # P(X≥5|n=10,p=.5) включает медиану
    assert FP._binom_sf_ge(28, 30) < 0.001         # сильный перекос значим
    # против высокой базы p0=0.8: 24/30 НЕ значимо, 29/30 значимо
    assert FP._binom_sf_ge(24, 30, 0.8) > 0.05
    assert FP._binom_sf_ge(29, 30, 0.8) < 0.05


def test_genuine_skill_over_balanced_base_promotes():
    # сбалансированные исходы (base≈0.5), модель угадывает 85% → реальный условный скилл
    probs, outs = _make(40, base_rate=0.5, accuracy=0.85, p_conf=0.8)
    rec = FP.promote_decision(probs, outs, beta_fullsample=1.2)
    assert rec["promote"] is True
    assert rec["base_rate"] == 0.5
    assert rec["bss"] > FP.MIN_BSS
    assert 0 < rec["reliability"] <= FP.FORWARD_RELIABILITY_CAP
    assert rec["beta_fullsample"] == 1.2
    assert "ПРОМОУШЕН" in rec["причина"]


def test_base_rate_coin_NOT_promoted():
    # §1.4/F0#5: терминал base-rate 0.8, модель лишь воспроизводит базу (accuracy=0.8) → НУЛЕВОЙ
    # условный скилл; раньше проходил (hit>0.5, Brier<0.25), теперь блокируется (hit не бьёт базу, BSS≤0)
    probs, outs = _make(40, base_rate=0.8, accuracy=0.8, p_conf=0.7)
    rec = FP.promote_decision(probs, outs)
    assert rec["promote"] is False
    assert rec["bss"] <= FP.MIN_BSS                 # не лучше климатологии
    assert "баз" in rec["причина"]


def test_below_min_outcomes_blocks_even_perfect():
    probs, outs = _make(20, base_rate=0.5, accuracy=1.0)   # идеал, но N<30
    rec = FP.promote_decision(probs, outs)
    assert rec["promote"] is False
    assert rec["enough_outcomes"] is False
    assert rec["reliability"] == 0.0
    assert "20<30" in rec["причина"]


def test_coinflip_not_significant():
    probs, outs = _make(40, base_rate=0.5, accuracy=0.5)   # 50% угадываний на сбаланс. базе
    rec = FP.promote_decision(probs, outs)
    assert rec["promote"] is False
    assert rec["skill_significant"] is False


def test_degenerate_base_no_skill_measurable():
    # все исходы одинаковы (base_rate=1.0) → climatology_brier=0 → скилл недоказуем → не промоутить
    probs = [0.9] * 35
    outs = [1] * 35
    rec = FP.promote_decision(probs, outs)
    assert rec["promote"] is False
    assert rec["climatology_brier"] == 0.0
    assert rec["bss"] is None


def test_p_exactly_half_excluded_from_direction():
    # p ровно 0.5 — нет направленного вызова, в hit-тест не идёт (F0#5/§2.7 «p==0.5 как вверх»)
    probs = [0.5] * 40
    outs = [1] * 20 + [0] * 20
    sk = FP.forward_skill(probs, outs)
    assert sk["n_directional"] == 0
    assert sk["skill_significant"] is False


def test_reliability_from_bss_capped():
    assert FP.reliability_from_skill(1.0) == FP.FORWARD_RELIABILITY_CAP
    assert FP.reliability_from_skill(0.5) == 0.5
    assert FP.reliability_from_skill(0.0) == 0.0
    assert FP.reliability_from_skill(-0.2) == 0.0   # хуже климатологии → 0
    assert FP.reliability_from_skill(None) == 0.0


def test_aggregate_and_promote_all():
    rows = []
    pa, oa = _make(40, base_rate=0.5, accuracy=0.85)       # реальный скилл → промоушен
    rows += [{"edge_key": "VRT.US->GEV.US", "probability": p, "outcome": o,
              "beta_fullsample": 1.1} for p, o in zip(pa, oa)]
    pb, ob = _make(10, base_rate=0.5, accuracy=1.0)        # мало исходов
    rows += [{"edge_key": "X->Y", "probability": p, "outcome": o} for p, o in zip(pb, ob)]
    rows.append({"edge_key": "X->Y", "probability": None, "outcome": 1})   # мусор (П8)
    res = FP.promote_all(rows)
    assert res["VRT.US->GEV.US"]["promote"] is True
    assert res["VRT.US->GEV.US"]["beta_fullsample"] == 1.1
    assert res["X->Y"]["promote"] is False
    assert res["X->Y"]["n"] == 10
