# -*- coding: utf-8 -*-
from mathlib import shelf_rules as R


def test_drawdown_from_year_high():
    rows = [(f"2026-01-{i:02d}", 100 + i) for i in range(1, 21)] + [("2026-02-01", 96)]
    d = R.drawdown(rows)
    assert d["максимум"] == 120 and d["просадка_проц"] == -20.0 and d["дата"] == "2026-02-01"
    assert R.drawdown(rows[:5]) is None


def test_status_order_thesis_first_then_entry_then_expensive():
    assert R.status(-30, 0.2, -0.15)[0] == "тезис под вопросом"          # резкий срез прогнозов главнее цены
    assert R.status(-25, 0.95, 0.02)[0] == "зона входа"
    assert R.status(-25, None, -0.07)[0] == "ждать"                    # просела, но прогнозы режут
    assert R.status(-25, None, None)[0] == "зона входа" and "нет в данных" in R.status(-25, None, None)[1][0]
    assert R.status(-5, 0.93, 0.0)[0] == "дорого"
    assert R.status(-5, 0.5, 0.0)[0] == "ждать"
    assert R.status(None, None, None) == ("ждать", ["нет данных о годовом максимуме"])
    assert R.status(-12, None, None, {"просадка_входа_проц": 10})[0] == "зона входа"


def test_next_review_after_earnings_or_quarter():
    assert R.next_review("2026-11-20", "2026-11-15") == "2026-11-22"
    assert R.next_review(None, "2026-11-15") == "2027-02-14"
    assert R.next_review("2026-10-01", "2026-11-15") == "2027-02-14"     # прошедший отчёт не в счёт
