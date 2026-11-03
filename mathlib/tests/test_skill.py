# -*- coding: utf-8 -*-
"""Умение против базы шума (mathlib/skill.py, точка Б)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from mathlib import skill as SK   # noqa: E402


def test_small_n_defers_verdict_but_reports_numbers():
    r = SK.brier_skill_vs_base([0.8, 0.2], [0.5, 0.5], [1, 0], min_n=40)
    assert r["n"] == 2 and r["verdict"] == "данных мало" and r["ci95"] is None
    assert abs(r["brier"] - 0.04) < 1e-9 and abs(r["brier_base"] - 0.25) < 1e-9
    assert abs(r["skill"] - (1 - 0.04 / 0.25)) < 1e-9


def test_clear_skill_and_clear_harm():
    good = SK.brier_skill_vs_base([0.9] * 30 + [0.1] * 30, [0.5] * 60, [1] * 30 + [0] * 30,
                                  clusters=[i // 3 for i in range(60)])
    assert good["verdict"] == "лучше шума" and good["ci95"][0] > 0
    bad = SK.brier_skill_vs_base([0.1] * 30 + [0.9] * 30, [0.5] * 60, [1] * 30 + [0] * 30)
    assert bad["verdict"] == "хуже шума" and bad["ci95"][1] < 0


def test_same_as_base_is_indistinguishable_and_deterministic():
    ps = [0.3, 0.6] * 25
    ys = [0, 1, 1, 0, 0] * 10
    a = SK.brier_skill_vs_base(ps, ps, ys)
    assert a["skill"] == 0.0 and a["verdict"] == "не отличить от шума"
    assert SK.brier_skill_vs_base(ps, [0.5] * 50, ys) == SK.brier_skill_vs_base(ps, [0.5] * 50, ys)


def test_garbage_rows_skipped():
    r = SK.brier_skill_vs_base([0.5, "x", 1.5, 0.4], [0.5, 0.5, 0.5, None], [1, 0, 1, 0])
    assert r["n"] == 1
    assert SK.brier_skill_vs_base([], [], [])["n"] == 0
