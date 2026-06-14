# -*- coding: utf-8 -*-
"""Тесты экономического лага на операционных данных (mathlib/calibration/operational_lags.py)."""
from mathlib.calibration import operational_lags as OL


def _fund(revenues):
    """fundamentals-заглушка: revenues = список (квартал, выручка)."""
    return {"Financials": {"Income_Statement": {"quarterly": {
        d: {"totalRevenue": str(v)} for d, v in revenues}}}}


def _quarters(n, start_year=2016):
    out, y, m = [], start_year, 3
    for _ in range(n):
        out.append(f"{y}-{m:02d}-{ {3:'31',6:'30',9:'30',12:'31'}[m] }")
        m += 3
        if m > 12:
            m = 3; y += 1
    return out


def test_revenue_series_parses_and_sorts():
    f = _fund([("2020-06-30", "200"), ("2020-03-31", "100")])
    s = OL.revenue_series(f)
    assert s == [("2020-03-31", 100.0), ("2020-06-30", 200.0)]


def test_yoy_growth_removes_seasonality():
    qs = _quarters(8)
    rev = [(qs[i], 100 * (1.1 ** i)) for i in range(8)]  # стабильный рост
    g = OL.yoy_growth(OL.revenue_series(_fund(rev)))
    assert len(g) == 4                       # 8 кварталов − 4 лага
    # YoY ≈ 1.1^4 − 1
    v = list(g.values())[0]
    assert abs(v - (1.1 ** 4 - 1)) < 1e-6


def test_lead_lag_recovers_known_quarter_lag():
    qs = _quarters(20)
    import math
    # x: волнообразный YoY; y = x, сдвинутый на 2 квартала (y отстаёт → x опережает на 2)
    xrev = [(qs[i], 1000 * (1 + 0.3 * math.sin(i))) for i in range(20)]
    yrev = [(qs[i], 1000 * (1 + 0.3 * math.sin(i - 2))) for i in range(20)]
    gx = OL.yoy_growth(OL.revenue_series(_fund(xrev)))
    gy = OL.yoy_growth(OL.revenue_series(_fund(yrev)))
    best = OL.lead_lag_quarters(gx, gy, max_lag=6)
    assert best is not None
    assert best["lag_quarters"] == 2         # x опережает y на 2 квартала
    assert abs(best["r"]) > 0.7


def test_insufficient_quarters_returns_none():
    qs = _quarters(6)
    rev = [(qs[i], 100 + i) for i in range(6)]
    g = OL.yoy_growth(OL.revenue_series(_fund(rev)))   # только 2 YoY-точки
    assert OL.lead_lag_quarters(g, g, max_lag=6) is None


def test_calibrate_operational_marks_power_and_fdr():
    qs = _quarters(40)
    import math
    xrev = [(qs[i], 1000 * (1 + 0.3 * math.sin(i))) for i in range(40)]
    yrev = [(qs[i], 1000 * (1 + 0.3 * math.sin(i - 1))) for i in range(40)]
    chain = {"id": "t", "nodes": [
        {"order": 1, "instruments": ["X.US"]}, {"order": 2, "instruments": ["Y.US"]}],
        "edges": [{"from": 1, "to": 2, "lag_days": 60}]}
    res = OL.calibrate_operational(chain, {"X.US": _fund(xrev), "Y.US": _fund(yrev)})
    e = res["edges"][0]
    assert e["lag_hypothesis_days"] == 60        # гипотеза сохранена
    assert e["operational"] is not None
    assert e["operational"]["lag_quarters"] == 1
    assert "power_note" in e["operational"] and "r_ci95" in e["operational"]
