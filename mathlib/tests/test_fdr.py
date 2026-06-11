# -*- coding: utf-8 -*-
"""Тесты FDR Бенджамини–Хохберга (§6, §23.1 п.6)."""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import fdr  # noqa: E402


def test_empty_input():
    r = fdr.benjamini_hochberg([], q=0.1)
    assert r["n_signif"] == 0 and r["rejected"] == [] and r["qvalues"] == []


def test_all_null_no_rejections():
    # крупные p-значения → ни одного сигнала (защита от шума §6)
    r = fdr.benjamini_hochberg([0.8, 0.9, 0.95, 0.99], q=0.1)
    assert r["n_signif"] == 0
    assert r["rejected"] == [False, False, False, False]
    assert r["threshold"] == 0.0


def test_classic_bh_example():
    # Канонический пример BH (m=4): p=[0.005,0.02,0.04,0.5], q=0.05
    # пороги k/m*q = 0.0125,0.025,0.0375,0.05 → проходят p1,p2 (p3=0.04>0.0375)
    r = fdr.benjamini_hochberg([0.005, 0.02, 0.04, 0.5], q=0.05)
    assert r["rejected"] == [True, True, False, False]
    assert r["n_signif"] == 2
    assert r["threshold"] == pytest.approx(0.02)


def test_step_up_rejects_below_largest_passing():
    # step-up: даже если средний p не проходит свою линию, он отклоняется,
    # если ниже есть прошедший. p=[0.01, 0.04, 0.03], m=3, q=0.05
    # отсортированы: 0.01(1/3*0.05=0.0167 ✓),0.03(0.0333 ✓),0.04(0.05 ✓) → все 3
    r = fdr.benjamini_hochberg([0.01, 0.04, 0.03], q=0.05)
    assert r["rejected"] == [True, True, True]
    assert r["n_signif"] == 3


def test_qvalues_monotone_and_in_unit_interval():
    p = [0.001, 0.2, 0.02, 0.7, 0.04]
    r = fdr.benjamini_hochberg(p, q=0.1)
    q = r["qvalues"]
    assert all(0.0 <= x <= 1.0 for x in q)
    # q-значения монотонны по возрастанию исходного p
    order = sorted(range(len(p)), key=lambda i: p[i])
    q_in_p_order = [q[i] for i in order]
    assert q_in_p_order == sorted(q_in_p_order)


def test_order_preserved():
    # результат возвращается в ИСХОДНОМ порядке входа
    r = fdr.benjamini_hochberg([0.5, 0.001, 0.9], q=0.1)
    assert r["rejected"][1] is True
    assert r["rejected"][0] is False and r["rejected"][2] is False


def test_invalid_pvalues():
    with pytest.raises(ValueError):
        fdr.benjamini_hochberg([0.5, 1.5], q=0.1)
    with pytest.raises(ValueError):
        fdr.benjamini_hochberg([[0.1, 0.2]], q=0.1)
