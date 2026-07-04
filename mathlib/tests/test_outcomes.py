# -*- coding: utf-8 -*-
"""Тесты сверки исходов (§10.10, §4 «Разборщик»)."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import outcomes as oc  # noqa: E402


def _pred(direction="above", threshold=90.0, prob=0.6):
    return {
        "asset": "Brent",
        "direction": direction,
        "threshold": threshold,
        "resolve_by": "2026-06-30T20:00:00+00:00",
        "price_source": "EODHD",
        "probability": prob,
    }


def test_above_hit_and_miss():
    r = oc.resolve_prediction(_pred("above", 90.0), 92.0, "2026-06-30T20:00:00+00:00")
    assert r["status"] == "resolved" and r["outcome"] == 1
    r = oc.resolve_prediction(_pred("above", 90.0), 88.0, "2026-07-01T00:00:00+00:00")
    assert r["status"] == "resolved" and r["outcome"] == 0


def test_below_hit_and_miss():
    r = oc.resolve_prediction(_pred("below", 90.0), 80.0, "2026-07-01T00:00:00+00:00")
    assert r["outcome"] == 1
    r = oc.resolve_prediction(_pred("below", 90.0), 95.0, "2026-07-01T00:00:00+00:00")
    assert r["outcome"] == 0


def test_exact_threshold_is_miss_both_sides():
    # Ревью 2026-07-04: «закроется ВЫШЕ $X» при закрытии РОВНО на $X — не сбылось (строгое
    # неравенство). Раньше равенство было успехом и для above, и для below — смещение в пользу системы.
    r = oc.resolve_prediction(_pred("above", 90.0), 90.0, "2026-07-01T00:00:00+00:00")
    assert r["status"] == "resolved" and r["outcome"] == 0
    r = oc.resolve_prediction(_pred("below", 90.0), 90.0, "2026-07-01T00:00:00+00:00")
    assert r["status"] == "resolved" and r["outcome"] == 0


def test_pending_before_deadline_does_not_invent_outcome():
    # П8: срок не наступил → исход не выдумываем
    r = oc.resolve_prediction(_pred(), 200.0, "2026-06-15T00:00:00+00:00")
    assert r["status"] == "pending" and r["outcome"] is None


def test_pending_when_no_data():
    r = oc.resolve_prediction(_pred(), None, "2026-07-01T00:00:00+00:00")
    assert r["status"] == "pending" and r["outcome"] is None
    r = oc.resolve_prediction(_pred(), 92.0, None)
    assert r["status"] == "pending"


def test_unknown_direction_is_error():
    r = oc.resolve_prediction(_pred("sideways"), 92.0, "2026-07-01T00:00:00+00:00")
    assert r["status"] == "error"


def test_reconcile_journal_and_brier_inputs():
    items = [
        (_pred("above", 90.0, 0.7), 92.0, "2026-07-01T00:00:00+00:00"),  # hit
        (_pred("below", 50.0, 0.4), 60.0, "2026-07-01T00:00:00+00:00"),  # miss
        (_pred("above", 90.0, 0.8), 100.0, "2026-06-15T00:00:00+00:00"),  # pending → пропуск
        (_pred("above", 90.0, None), 92.0, "2026-07-01T00:00:00+00:00"),  # без prob → пропуск
    ]
    resolved = oc.reconcile_journal(items)
    probs, outs = oc.to_brier_inputs(resolved)
    assert probs == [0.7, 0.4]
    assert outs == [1, 0]
