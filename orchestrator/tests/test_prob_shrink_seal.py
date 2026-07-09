# -*- coding: utf-8 -*-
"""П-1 (подпись 09.07): сжатие official probability при печати каскадных треков;
сырая уверенность сохраняется в probability_raw; edge_forward вне applies_to."""
from orchestrator import cascade_resolve as CR
from mathlib.calibration import prob_shrink as PS

POLICY = {"applies_to": ["cascade_money", "cascade_provisional"], "lambda": 0.0, "p0": 0.431,
          "fitted_at": "2026-07-09"}
FACT = {"symbol": "GEV.US", "amplitude": 0.05, "probability": 0.8, "reliability": 0.2,
        "tiers": ["A"], "path_edges": [], "research": True}


def _seal(monkeypatch, kind, policy):
    monkeypatch.setattr(CR, "_latest_close", lambda s, c: {"date": "2026-07-09", "close": 100.0})
    monkeypatch.setattr(PS, "load_policy", lambda path=None: policy)
    monkeypatch.setattr(CR.SEAL, "validate_resolvable", lambda p: None)
    return CR.seal_spec(FACT, kind=kind, run_id="t", horizon_days=5, con=None)


def test_shrink_applied_to_cascade_tracks(monkeypatch):
    pred = _seal(monkeypatch, "cascade_provisional", POLICY)
    assert pred["probability_raw"] == 0.8            # сырая сохранена (для промоушена/пере-подгонки)
    assert pred["probability"] == 0.431              # λ=0 → базовая частота
    assert pred["prob_shrink"]["lambda"] == 0.0


def test_no_policy_prints_raw(monkeypatch):
    pred = _seal(monkeypatch, "cascade_provisional", None)
    assert pred["probability"] == 0.8 and pred["probability_raw"] is None


def test_edge_forward_out_of_scope(monkeypatch):
    pred = _seal(monkeypatch, "edge_forward", POLICY)
    assert pred["probability"] == 0.8 and pred["probability_raw"] is None
