# -*- coding: utf-8 -*-
"""Аудит 07.11: время события/сессия (находка №1) и воспроизводимый расчёт цели (№4–5)."""
import pytest

from mathlib import event_timing as ET
from mathlib import target_calc as TC


# ── время события ─────────────────────────────────────────────────────────────
def test_mp_report_after_close_reaction_is_next_day():
    # регрессия MP 05.11.2026: отчёт после закрытия (21:05 UTC = 16:05 Нью-Йорк, EST) —
    # падение 05.11 было ДО отчёта; реакция — сессия 06.11
    c = ET.classify("2026-11-05T21:05:00Z")
    assert c["session"] == "after_close"
    assert c["reaction_day"] == "2026-11-06"
    assert c["last_close_before"] == "2026-11-05"
    assert c["tz_offset_h"] == -5


def test_dst_summer_offset_and_regular_session():
    c = ET.classify("2026-08-06T14:00:00Z")          # 10:00 EDT
    assert c["tz_offset_h"] == -4 and c["session"] == "regular"
    assert c["reaction_day"] == "2026-08-06" and c["partial_reaction"] is True


def test_pre_market_and_weekend():
    assert ET.classify("2026-11-09T12:00:00Z")["session"] == "pre_market"   # 07:00 EST понедельник
    w = ET.classify("2026-11-07T15:00:00Z")                                  # суббота
    assert w["session"] == "non_trading" and w["reaction_day"] == "2026-11-09"
    assert w["last_close_before"] == "2026-11-06"


def test_holiday_skipped_when_known():
    c = ET.classify("2026-11-25T22:00:00Z", holidays=("2026-11-26",))       # среда после закрытия, четверг — праздник
    assert c["reaction_day"] == "2026-11-27"


def test_reaction_splits_pre_and_post_moves():
    closes = {"2026-11-04": 35.86, "2026-11-05": 33.74, "2026-11-06": 35.70}
    r = ET.reaction("2026-11-05T21:05:00Z", closes)
    assert r["pre_move_pct"] == pytest.approx(-5.91, abs=0.01)      # ход ДО отчёта
    assert r["reaction_move_pct"] == pytest.approx(5.81, abs=0.01)  # реакция на отчёт
    assert "гипотеза" in r["text"] and not r["missing"]


def test_naive_time_refused_and_session_match():
    with pytest.raises(ValueError):
        ET.classify("2026-11-05T21:05:00")
    ok, got = ET.session_matches("regular", "2026-11-05T21:05:00Z")
    assert not ok and got == "after_close"


def test_missing_closes_are_honest():
    r = ET.reaction("2026-11-05T21:05:00Z", {"2026-11-05": 33.74})
    assert r["reaction_move_pct"] is None and "2026-11-06" in r["missing"]


# ── расчёт цели ───────────────────────────────────────────────────────────────
def test_evaluate_whitelist_only():
    assert TC.evaluate("eps * pe", {"eps": 2.0, "pe": 12}) == 24.0
    assert TC.evaluate("max(a, b) - 1", {"a": 3, "b": 5}) == 4.0
    for bad in ("__import__('os')", "a.real", "'x'", "a if a else b"):
        with pytest.raises(TC.CalcError):
            TC.evaluate(bad, {"a": 1, "b": 2})
    with pytest.raises(TC.CalcError):
        TC.evaluate("a / b", {"a": 1, "b": 0})
    with pytest.raises(TC.CalcError):
        TC.evaluate("a * c", {"a": 1})


def test_no_basis_is_provisional():
    r = TC.check_basis(None, 47.5)
    assert r["status"] == "предварительная" and r["reasons"] and not r["fails"]


def test_mismatched_formula_is_a_fail():
    b = {"method": "yield", "formula": "div_annual / y", "inputs": {"div_annual": 4.0, "y": 0.2},
         "input_sources": {"div_annual": "x", "y": "x"}, "assumptions": ["a"]}
    r = TC.check_basis(b, 27.0)                      # 4/0.2 = 20 ≠ 27
    assert r["fails"] and r["computed"] == 20.0


def test_external_target_and_thin_analogy_stay_provisional():
    ext = {"method": "external_target", "formula": "t", "inputs": {"t": 263}, "input_sources": {"t": "BMO"},
           "assumptions": ["x"]}
    assert TC.check_basis(ext, 263)["status"] == "предварительная"
    an = {"method": "event_analogy", "formula": "p * (1 + m)", "inputs": {"p": 200, "m": 0.15},
          "input_sources": {"p": "close", "m": "median"}, "assumptions": ["x"],
          "sample": [{"episode": "2025-04", "move_pct": 34.7}], "comparability": "тот же канал"}
    r = TC.check_basis(an, 230)
    assert r["status"] == "предварительная" and any("аналогий" in x for x in r["reasons"])


def test_full_basis_is_grounded():
    b = {"method": "yield", "formula": "div_q * 4 / y", "inputs": {"div_q": 1.5, "y": 0.2},
         "input_sources": {"div_q": "6-K", "y": "история доходности"}, "assumptions": ["4 квартала подряд"]}
    r = TC.check_basis(b, 30.0)
    assert r["status"] == "обоснована" and not r["fails"]
