# -*- coding: utf-8 -*-
"""Аудит 07.11 (№6): три раздельных итога идеи — прогноз, исполнимость входа, модельная сделка."""
from mathlib import idea_outcome as IO

CARD = {"direction": "вверх", "entry_limit": 40.3, "target_price": 47.5, "stop_price": 35.5,
        "spec9": {"direction": "above", "threshold": 47.5, "resolve_by": "2026-11-25"}}


def test_never_filled_entry_means_no_trade_but_forecast_still_scored():
    closes = {"2026-11-10": 41.0, "2026-11-24": 49.0, "2026-11-25": 48.0}
    r = IO.evaluate(CARD, closes, "2026-11-10")
    assert r["прогноз"]["status"] == "сбылось"
    assert r["вход"]["filled"] is False and r["сделка"]["status"] == "сделки не было"


def test_fill_then_stop():
    closes = {"2026-11-10": 39.0, "2026-11-12": 34.9, "2026-11-25": 48.0}
    r = IO.evaluate(CARD, closes, "2026-11-10")
    assert r["вход"]["date"] == "2026-11-10" and r["сделка"]["status"] == "стоп"
    assert r["сделка"]["pnl_pct"] < 0 and r["прогноз"]["status"] == "сбылось"   # итоги не смешиваются


def test_fill_then_deadline_and_pending():
    r = IO.evaluate(CARD, {"2026-11-10": 39.0, "2026-11-25": 41.0}, "2026-11-10")
    assert r["сделка"]["status"] == "крайний срок" and r["прогноз"]["status"] == "не сбылось"
    p = IO.evaluate(CARD, {"2026-11-10": 39.0}, "2026-11-10")
    assert p["прогноз"]["status"] == "ждёт" and p["сделка"]["status"] == "открыта"
