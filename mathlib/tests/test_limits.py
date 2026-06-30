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


def test_paper_to_money_gate_from_config():
    # F2#22: единый источник гейта из config (не хардкод 270 в resolve/calibrate)
    assert lm.paper_to_money_gate() == lm.gates()["paper_to_money_predictions"] == 270


def test_kill_band_breach_is_deterministic_kill():
    # калибровка хуже kill-порога (15 п.п.) → KILL объявляется КОДОМ
    k = lm.check_kill_criteria(calibration_band_pp=22.0, n_money_resolved=10)
    assert k["kill"] is True
    assert any("калибровка" in r for r in k["reasons"])
    # в пределах нормы → не KILL
    assert lm.check_kill_criteria(calibration_band_pp=8.0, n_money_resolved=10)["kill"] is False


def test_kill_no_edge_only_after_threshold():
    # до порога прогнозов edge-KILL НЕ применяется (П8: мало данных ≠ KILL)
    early = lm.check_kill_criteria(n_money_resolved=50, money_brier=0.30, money_base_rate=0.5)
    assert early["kill"] is False
    assert "не применимо" in early["checks"]["edge"]["статус"]
    # после порога: нет скилла над климатологией (BSS≤0) → KILL
    no_edge = lm.check_kill_criteria(n_money_resolved=300, money_brier=0.26, money_base_rate=0.5)
    assert no_edge["kill"] is True and no_edge["checks"]["edge"]["нет_edge"] is True
    # после порога: есть скилл (BSS>0) → не KILL
    edge = lm.check_kill_criteria(n_money_resolved=300, money_brier=0.10, money_base_rate=0.5)
    assert edge["kill"] is False and edge["checks"]["edge"]["bss_над_климатологией"] > 0


def test_kill_band_not_measurable_is_not_kill():
    # band=None (нет корзин с N, F2#20) → не KILL, статус «не измерима» (П8)
    k = lm.check_kill_criteria(calibration_band_pp=None, n_money_resolved=5)
    assert k["kill"] is False and "не измерима" in k["checks"]["калибровка"]["статус"]


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
