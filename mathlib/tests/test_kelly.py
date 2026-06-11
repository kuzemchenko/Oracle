# -*- coding: utf-8 -*-
"""Тесты Келли с shrinkage и размера позиции (§4 портфель, §11)."""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import kelly as k  # noqa: E402


def test_shrink_probability():
    # калибровка доказана полностью → без стягивания
    assert k.shrink_probability(0.8, 1.0) == pytest.approx(0.8)
    # не доказана → к 0.5
    assert k.shrink_probability(0.8, 0.0) == pytest.approx(0.5)
    # наполовину → на полпути к 0.5
    assert k.shrink_probability(0.8, 0.5) == pytest.approx(0.65)
    # клиппинг calibration_proven
    assert k.shrink_probability(0.8, 2.0) == pytest.approx(0.8)
    with pytest.raises(ValueError):
        k.shrink_probability(1.2, 1.0)


def test_kelly_fraction_known_value():
    # p=0.6, b=1 → f* = (1*0.6 - 0.4)/1 = 0.2
    assert k.kelly_fraction(0.6, 1.0) == pytest.approx(0.2)
    # p=0.6, b=2 → (2*0.6 - 0.4)/2 = 0.4
    assert k.kelly_fraction(0.6, 2.0) == pytest.approx(0.4)


def test_kelly_no_edge_clipped_to_zero():
    assert k.kelly_fraction(0.4, 1.0) == 0.0   # отрицательный край → не ставим
    assert k.kelly_fraction(0.5, 1.0) == 0.0
    with pytest.raises(ValueError):
        k.kelly_fraction(0.6, 0.0)


def test_position_size_fixed_microsize_before_gate():
    # §11: до gate калибровки — ФИКС 0.5% капитала/идея, Келли не применяется
    r = k.position_size(0.9, 2.0, 100000, gate_passed=False, microsize_pct=0.5)
    assert r["method"] == "fixed_microsize"
    assert r["amount_usd"] == pytest.approx(500.0)
    assert r["fraction"] == pytest.approx(0.005)
    assert r["p_used"] is None


def test_position_size_kelly_after_gate_with_shrinkage():
    # gate пройден, калибровка НЕ доказана (proven=0) → p стянута к 0.5 → нет края → 0
    r = k.position_size(0.9, 1.0, 100000, gate_passed=True, calibration_proven=0.0)
    assert r["method"] == "fractional_kelly"
    assert r["p_used"] == pytest.approx(0.5)
    assert r["amount_usd"] == pytest.approx(0.0)
    # калибровка доказана, дробный Келли 0.5: p=0.6,b=1 → f*=0.2 → *0.5 = 0.1
    r2 = k.position_size(0.6, 1.0, 100000, gate_passed=True, calibration_proven=1.0,
                         kelly_multiplier=0.5)
    assert r2["kelly_full"] == pytest.approx(0.2)
    assert r2["fraction"] == pytest.approx(0.1)
    assert r2["amount_usd"] == pytest.approx(10000.0)


def test_position_size_max_pct_cap():
    r = k.position_size(0.99, 5.0, 100000, gate_passed=True, calibration_proven=1.0,
                        kelly_multiplier=1.0, max_pct=2.0)
    assert r["fraction"] <= 0.02 + 1e-12
    assert r["amount_usd"] == pytest.approx(2000.0)


def test_position_size_validates_capital():
    with pytest.raises(ValueError):
        k.position_size(0.6, 1.0, 0, gate_passed=False)
