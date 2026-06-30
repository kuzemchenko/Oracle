# -*- coding: utf-8 -*-
"""Тесты двустороннего t-хвоста (mathlib/tailprob.py, F2#19) против известных эталонов."""
import math

from mathlib import tailprob as TP


def test_cauchy_df1_exact():
    # t(1) = Коши: P(|T| ≥ 1) = 0.5 ровно; P(|T| ≥ tan(0.4π)) = 0.2
    assert abs(TP.student_t_two_sided_p(1.0, 1) - 0.5) < 1e-6
    assert abs(TP.student_t_two_sided_p(math.tan(0.4 * math.pi), 1) - 0.2) < 1e-4


def test_t_critical_values_give_0_05():
    # Стандартные двусторонние 5%-критические значения t
    assert abs(TP.student_t_two_sided_p(2.228, 10) - 0.05) < 1e-3
    assert abs(TP.student_t_two_sided_p(2.571, 5) - 0.05) < 1e-3
    assert abs(TP.student_t_two_sided_p(2.776, 4) - 0.05) < 1e-3


def test_large_df_approaches_normal():
    # df→∞ → нормаль: P(|Z|≥1.96)=0.05, P(|Z|≥2.576)=0.01
    assert abs(TP.student_t_two_sided_p(1.96, 100000) - 0.05) < 2e-3
    assert abs(TP.student_t_two_sided_p(2.576, 100000) - 0.01) < 2e-3


def test_heavier_tails_give_larger_p_than_normal():
    # При одном z тяжёлый хвост (малый df) даёт БОЛЬШИЙ p, чем нормаль (= честнее на FDR)
    z = 3.5
    p_t5 = TP.student_t_two_sided_p(z, 5)
    p_norm = math.erfc(abs(z) / math.sqrt(2))
    assert p_t5 > p_norm


def test_bounds_and_symmetry():
    assert TP.student_t_two_sided_p(0.0, 5) == 1.0
    assert 0.0 <= TP.student_t_two_sided_p(50.0, 3) <= 1.0
    assert abs(TP.student_t_two_sided_p(2.1, 7) - TP.student_t_two_sided_p(-2.1, 7)) < 1e-12
