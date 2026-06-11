# -*- coding: utf-8 -*-
"""Тесты программной проверки лимитов и бюджетов (§11, §12, инвариант 5).
Читают реальный config/limits.yaml — проверяют, что зашитые потолки соблюдаются кодом."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import limits as lm  # noqa: E402


def test_limits_load_known_anchors():
    lim = lm.load_limits()
    assert lim["risk"]["per_idea_microsize_usd"] == 500
    assert lim["risk"]["monthly_risk_cap_usd"] == 3000
    assert lim["risk"]["fast_basket_usd"] == 300
    assert lim["budget"]["total_usd_month"] == 700
    # денежные ворота §11 присутствуют и не пусты
    assert lim["gates"]["paper_to_money_predictions"] == 270


def test_idea_risk():
    assert lm.check_idea_risk(500)["allowed"] is True
    assert lm.check_idea_risk(500.01)["allowed"] is False
    assert lm.check_idea_risk(-1)["allowed"] is False


def test_monthly_risk_cap():
    assert lm.check_monthly_risk(2500, 500)["allowed"] is True
    r = lm.check_monthly_risk(2900, 500)
    assert r["allowed"] is False and r["would_be"] == 3400


def test_fast_basket_non_replenishable():
    assert lm.check_fast_basket(200, 100)["allowed"] is True
    assert lm.check_fast_basket(250, 100)["allowed"] is False


def test_monthly_budget_levels():
    assert lm.check_monthly_budget(100)["level"] == "ok"
    # 80% от 700 = 560 → alert, но ещё allowed
    a = lm.check_monthly_budget(600)
    assert a["allowed"] is True and a["level"] == "alert"
    # ≥ потолка → стоп
    s = lm.check_monthly_budget(700)
    assert s["allowed"] is False and s["level"] == "stop"


def test_run_token_budget():
    assert lm.check_run_token_budget("funnel_full", 8.0)["allowed"] is True
    assert lm.check_run_token_budget("funnel_full", 8.01)["allowed"] is False
    assert lm.check_run_token_budget("theme_daily", 1.5)["allowed"] is True
    assert lm.check_run_token_budget("unknown_mode", 0.1)["allowed"] is False


def test_overrides_via_injected_limits():
    # лимиты можно подменить (тестовая изоляция), но дефолт читает config
    fake = {"risk": {"per_idea_microsize_usd": 10}}
    assert lm.check_idea_risk(20, limits=fake)["allowed"] is False
