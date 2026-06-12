# -*- coding: utf-8 -*-
"""Тесты детерминированного скоринга §7 (mathlib/scoring.py)."""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import scoring as SC  # noqa: E402


def test_weights_load_and_sum_to_one():
    w, ver = SC.load_scoring_weights()
    assert set(w) == set(SC.CRITERIA)
    assert abs(sum(w.values()) - 1.0) < 1e-9  # §30: 22/22/18/14/14/10
    assert ver is not None


def test_score_idea_weighted_sum():
    # все критерии = 1 → балл = сумма весов = 1.0
    vals = {c: 1.0 for c in SC.CRITERIA}
    r = SC.score_idea(vals)
    assert abs(r["total"] - 1.0) < 1e-9
    # все 0 → 0
    assert SC.score_idea({c: 0.0 for c in SC.CRITERIA})["total"] == 0.0


def test_score_idea_breakdown_matches_weight_times_value():
    w, _ = SC.load_scoring_weights()
    vals = {c: 0.5 for c in SC.CRITERIA}
    r = SC.score_idea(vals)
    for c in SC.CRITERIA:
        assert abs(r["breakdown"][c] - w[c] * 0.5) < 1e-9


def test_score_idea_rejects_none_criterion():
    vals = {c: 0.5 for c in SC.CRITERIA}
    vals["asymmetry_net"] = None
    with pytest.raises(ValueError):
        SC.score_idea(vals)


def test_score_idea_rejects_out_of_range():
    vals = {c: 0.5 for c in SC.CRITERIA}
    vals["non_obviousness"] = 1.5
    with pytest.raises(ValueError):
        SC.score_idea(vals)


def test_min_score_gate():
    vals = {c: 0.3 for c in SC.CRITERIA}  # total = 0.3
    assert SC.score_idea(vals, min_score=0.5)["passes"] is False
    assert SC.score_idea(vals, min_score=0.2)["passes"] is True


def test_net_asymmetry_positive_ev_above_half():
    # высокая вероятность + большой выигрыш, малые издержки → score > 0.5, EV > 0
    r = SC.net_asymmetry_score(0.7, round_trip_bps=10, win_move_bps=400, loss_move_bps=200)
    assert r["ev_bps"] > 0 and r["score"] > 0.5
    assert r["borrow_assumed_zero"] is True  # borrow не подан → допущение 0


def test_net_asymmetry_short_borrow_no_data_flagged():
    # для шорта borrow=None → издержки занижены, флаг и note выставлены (П8)
    r = SC.net_asymmetry_score(0.6, 10, win_move_bps=300, loss_move_bps=150, short_borrow_bps=None)
    assert r["borrow_assumed_zero"] is True
    assert r["borrow_note"] and "ЗАНИЖЕН" in r["borrow_note"]
    # с поданным borrow EV ниже (издержки выше)
    r2 = SC.net_asymmetry_score(0.6, 10, win_move_bps=300, loss_move_bps=150, short_borrow_bps=5, horizon_days=10)
    assert r2["ev_bps"] < r["ev_bps"]
