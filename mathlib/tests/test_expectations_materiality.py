# -*- coding: utf-8 -*-
"""Тесты «что рынок уже ждёт» (обратный DCF, консенсус EODHD) и существенности эффекта (07.11)."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mathlib import expectations as E   # noqa: E402
from mathlib import materiality as M    # noqa: E402


# ── обратный DCF ─────────────────────────────────────────────────────────────────────
def test_pv_closed_form_zero_growth():
    # 100/год без роста при r=10%, g=0 стоит ровно 1000 при любом горизонте (сумма + хвост = 100/0.1)
    for n in (0, 1, 5, 10):
        assert abs(E._pv(0.0, 100.0, 0.10, 0.0, n) - 1000.0) < 1e-6


def test_implied_growth_recovers_zero_and_roundtrips():
    res = E.implied_growth(1000.0, 100.0, r=0.10, g=0.0, years=10)
    assert res["ok"] and abs(res["рост_в_год"]) < 1e-4 and res["pe_сейчас"] == 10.0
    # дороже → рынок ждёт роста; подстановка найденного роста возвращает цену
    res2 = E.implied_growth(2500.0, 100.0, r=0.10, g=0.02, years=10)
    x = res2["рост_в_год"]
    assert x > 0.05 and abs(E._pv(x, 100.0, 0.10, 0.02, 10) - 2500.0) < 3.0   # рост округлён до 4 знаков
    assert res2["входы"]["r"] == 0.10 and res2["метод"].startswith("обратный DCF")


def test_implied_growth_honest_refusals():
    assert not E.implied_growth(1000, -5)["ok"]                       # убыток → метод по выручке
    assert "выручке" in E.implied_growth(1000, -5)["причина"]
    assert not E.implied_growth(1000, 10, r=0.02, g=0.03)["ok"]       # r ≤ g
    far = E.implied_growth(1e9, 1.0)                                  # P/E миллиард — вне модели
    assert not far["ok"] and far["граница"] == "выше"


def test_revealed_expectations_revolut_like():
    # капитализация $160 млрд при чистой прибыли $1,7 млрд: рынок ждёт роста прибыли ~25–35%/год 10 лет
    res = E.implied_growth(160_000, 1_700, r=0.09, g=0.025, years=10)
    assert res["ok"] and 0.2 < res["рост_в_год"] < 0.4
    # при $115 млрд — ниже
    assert E.implied_growth(115_000, 1_700)["рост_в_год"] < res["рост_в_год"]


def test_implied_revenue_growth_with_margin_ramp():
    res = E.implied_revenue_growth(10_000, 2_000, terminal_margin=0.20, current_margin=-0.05, years=10)
    assert res["ok"] and res["ps_сейчас"] == 5.0 and res["рост_в_год"] > 0
    assert not E.implied_revenue_growth(10_000, 2_000, terminal_margin=0)["ok"]


# ── консенсус EODHD ──────────────────────────────────────────────────────────────────
TREND = {
    "2026-12-31": {"date": "2026-12-31", "period": "0y", "earningsEstimateAvg": "2.00",
                   "earningsEstimateLow": "1.50", "earningsEstimateHigh": "2.50",
                   "earningsEstimateNumberOfAnalysts": "12", "revenueEstimateAvg": "1000000000",
                   "epsTrendCurrent": "2.00", "epsTrend90daysAgo": "1.60", "epsRevisionsUpLast30days": "3"},
    "2027-12-31": {"date": "2027-12-31", "period": "+1y", "earningsEstimateAvg": "2.50",
                   "earningsEstimateLow": None, "epsTrend90daysAgo": "NA"},
}


def test_parse_trend_and_consensus_growth():
    t = E.parse_earnings_trend(TREND)
    assert t["0y"]["eps_avg"] == 2.0 and t["0y"]["eps_analysts"] == 12.0
    assert t["0y"]["пересмотр_90д"] == 0.25 and t["0y"]["разброс_eps"] == 0.5
    assert t["+1y"]["eps_low"] is None and t["+1y"]["пересмотр_90д"] is None   # П8: нет данных ≠ 0
    assert E.consensus_growth(t) == 0.25
    assert E.consensus_growth({"0y": {"eps_avg": -1}, "+1y": {"eps_avg": 1}}) is None


def test_compare_and_years_to_justify():
    c = E.compare(0.30, 0.18)
    assert c["ok"] and c["разрыв_пп"] == 12.0 and "БОЛЬШЕ" in c["вывод"]
    assert "совпадает" in E.compare(0.19, 0.18)["вывод"]
    assert not E.compare(None, 0.18)["ok"]
    n = E.years_to_justify(2500.0, 100.0, 0.15, r=0.10, g=0.02)
    assert n is not None and E._pv(0.15, 100.0, 0.10, 0.02, n) >= 2500.0
    assert n == 0 or E._pv(0.15, 100.0, 0.10, 0.02, n - 1) < 2500.0
    assert E.pct(0.123) == "+12,3%" and E.pct(None) == "нет данных"


# ── существенность ──────────────────────────────────────────────────────────────────
def test_materiality_grades_and_basis():
    big = M.materiality(effect_profit_per_year=50, years=5, net_income=200, market_cap=1500)  # 194.5/1500 = 13%
    assert big["ok"] and big["основание"] == "доля_капитализации" and big["вердикт"] == M.СУЩЕСТВЕННО
    giant = M.materiality(effect_profit_per_year=50, net_income=100_000)
    assert giant["вердикт"] == M.ШУМ and giant["основание"] == "доля_прибыли"
    loss = M.materiality(effect_revenue_per_year=30, revenue=1000, net_income=-50)
    assert loss["основание"] == "доля_выручки" and loss["вердикт"] == M.ПОГРАНИЧНЫЙ
    assert loss["мерила"]["доля_прибыли"] is None


def test_materiality_refusal_and_short_effect():
    assert not M.materiality(effect_profit_per_year=10)["ok"]
    short = M.materiality(effect_profit_per_year=100, years=0.5, net_income=200)
    assert short["вердикт"] == M.СУЩЕСТВЕННО and "короче года" in short["оговорка"]
    assert abs(M.effect_value(100, 1, r=0.10) - 90.91) < 0.01
