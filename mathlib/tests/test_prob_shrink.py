# -*- coding: utf-8 -*-
"""Тесты сжатия вероятностей к базовой частоте (mathlib/calibration/prob_shrink.py, §10)."""
import math

import pytest

from mathlib.calibration import prob_shrink as PS


def test_shrink_formula_and_clip():
    assert PS.shrink(0.8, 0.4, 1.0) == pytest.approx(0.8)   # λ=1 — доверяем себе полностью
    assert PS.shrink(0.8, 0.4, 0.0) == pytest.approx(0.4)   # λ=0 — только базовая частота
    assert PS.shrink(0.8, 0.4, 0.5) == pytest.approx(0.6)
    assert PS.shrink(0.999, 0.99, 1.0) == 0.99      # обрезка сверху
    assert PS.shrink(0.001, 0.01, 1.0) == 0.01      # обрезка снизу


def test_fit_lambda_overconfident_sample_prefers_shrink():
    """Самоуверенная выборка (P=0.8, сбывается 40%) → оптимум ближе к нулю, чем к единице."""
    probs = [0.8] * 100
    outs = [1] * 40 + [0] * 60
    lam, b, p0 = PS.fit_lambda(probs, outs)
    assert lam is not None and lam <= 0.2
    assert p0 == 0.4
    assert b <= PS.brier_score(probs, outs)          # не хуже сырых


def test_fit_lambda_calibrated_sample_keeps_confidence():
    """Хорошо откалиброванная выборка → λ близко к 1 (сжатие не навязывается)."""
    probs, outs = [], []
    for p, n in ((0.9, 50), (0.2, 50)):
        probs += [p] * n
        outs += [1] * round(p * n) + [0] * (n - round(p * n))
    lam, _, _ = PS.fit_lambda(probs, outs)
    assert lam >= 0.8


def test_fit_lambda_min_n():
    lam, b, p0 = PS.fit_lambda([0.7] * 10, [1] * 10)
    assert lam is None and b is None and p0 is None


def test_walkforward_protocol_honest_split():
    """OOS-протокол: подгонка на ранних, оценка на поздних; улучшение на самоуверенных данных."""
    rows = [(0.8, 1 if i % 5 < 2 else 0, f"2026-06-{10 + i // 10:02d}T00:00:00") for i in range(100)]
    r = PS.fit_lambda_walkforward(rows)
    assert r["применимо"] is True
    assert r["n_fit"] == 60 and r["n_oos"] == 40
    assert r["brier_oos_со_сжатием"] <= r["brier_oos_без_сжатия"]
    assert r["улучшение_oos"] >= 0
    assert not math.isnan(r["brier_oos_монетка_0.5"])


def test_walkforward_refuses_small_sample():
    rows = [(0.7, 1, "2026-06-01T00:00:00")] * 20
    r = PS.fit_lambda_walkforward(rows)
    assert r["применимо"] is False and "§10" in r["причина"]
