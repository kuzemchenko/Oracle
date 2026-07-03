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


def test_kill_band_breach_only_after_threshold():
    # §11 «после 270 разрешённых прогнозов»: калибровка > 15 п.п. до порога — НЕ KILL (П8)
    early = lm.check_kill_criteria(calibration_band_pp=22.0, n_money_resolved=10)
    assert early["kill"] is False and early["checks"]["порог_применимости"]["применимо"] is False
    # ЗА порогом та же калибровка хуже kill-порога → KILL объявляется КОДОМ (канон §11)
    k = lm.check_kill_criteria(calibration_band_pp=22.0, n_money_resolved=300)
    assert k["kill"] is True
    assert any("калибровка" in r for r in k["reasons"])
    # за порогом, но в пределах нормы → не KILL
    assert lm.check_kill_criteria(calibration_band_pp=8.0, n_money_resolved=300)["kill"] is False


def test_kill_not_applicable_before_threshold():
    # до порога прогнозов НИ одна ветка не даёт KILL (П8: мало данных ≠ KILL)
    early = lm.check_kill_criteria(n_money_resolved=50, money_brier=0.30, money_base_rate=0.5,
                                   calibration_band_pp=99.0)
    assert early["kill"] is False
    assert early["checks"]["порог_применимости"]["применимо"] is False
    assert "не применим" in early["checks"]["статус"]


def test_edge_branch_is_diagnostic_not_kill():
    # edge = ПРОКСИ (скилл над климатологией), бенчмарк-контур §30 не подключён → НЕ ставит kill
    # даже за порогом и при отрицательном скилле (BSS≤0) канон §11 требует бенчмарк, а не климатологию
    no_edge = lm.check_kill_criteria(n_money_resolved=300, money_brier=0.26, money_base_rate=0.5)
    assert no_edge["kill"] is False
    assert no_edge["checks"]["edge_диагностика"]["влияет_на_kill"] is False
    # диагностика считается, но остаётся справочной
    assert no_edge["checks"]["edge_диагностика"]["bss_над_климатологией"] is not None
    edge = lm.check_kill_criteria(n_money_resolved=300, money_brier=0.10, money_base_rate=0.5)
    assert edge["checks"]["edge_диагностика"]["bss_над_климатологией"] > 0


def test_kill_threshold_not_fail_open_without_edge_key():
    # fail-open защита: удаление kill_no_edge_after_predictions НЕ должно глушить калибровочный KILL.
    # Порог применимости берётся с дефолтом (paper_to_money_predictions → 270).
    fake = {"gates": {"kill_calibration_band_pp": 15, "paper_to_money_predictions": 270}}
    k = lm.check_kill_criteria(calibration_band_pp=99.0, n_money_resolved=100000, limits=fake)
    assert k["checks"]["порог_применимости"]["порог"] == 270
    assert k["kill"] is True and any("калибровка" in r for r in k["reasons"])
    # совсем без gates — дефолт 270, до порога не KILL
    empty = lm.check_kill_criteria(calibration_band_pp=99.0, n_money_resolved=5, limits={"gates": {}})
    assert empty["checks"]["порог_применимости"]["порог"] == 270 and empty["kill"] is False


def test_kill_band_not_measurable_is_not_kill():
    # band=None за порогом (нет корзин с N, F2#20) → не KILL, статус «не измерима» (П8)
    k = lm.check_kill_criteria(calibration_band_pp=None, n_money_resolved=300)
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
