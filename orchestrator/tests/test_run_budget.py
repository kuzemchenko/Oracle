# -*- coding: utf-8 -*-
"""Тесты пред-проверки бюджета прогона (MASTER_SPEC §24, долг Нед.8).

Проверяем оба контура: (1) ПРЕД-оценка отказывает ДО вызовов при превышении потолка режима
или месячного потолка; (2) RunBudgetGuard рвёт прогон на лету при пересечении потолка.
Инвариант 5 CLAUDE.md: превышение не обсуждается — функция возвращает allowed=False.
"""
import json
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator import run_budget as RB  # noqa: E402


def _limits():
    return {
        "budget": {"total_usd_month": 700, "tokens_usd_month": 500, "data_usd_month": 200,
                   "alert_fraction": 0.8, "costs_log": "journal/costs.jsonl"},
        "per_run_token_budget_usd": {"funnel_full": 8.0, "masked_smoke": 3.0},
        "per_run_expected_calls": {"funnel_full": 70, "masked_smoke": 5},
        "cost_per_call_prior_usd": 0.04,
    }


def _costs_file(tmp_path, rows):
    p = tmp_path / "costs.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return p


# ── Оценка стоимости ─────────────────────────────────────────────────────────────
def test_estimate_uses_prior_when_no_history(tmp_path):
    log = _costs_file(tmp_path, [])
    est = RB.estimate_run_cost("funnel_full", limits=_limits(), costs_log=log, month="2026-06")
    assert est["basis_avg"] == "приор"
    assert est["estimate_usd"] == pytest.approx(70 * 0.04)


def test_estimate_uses_history_average(tmp_path):
    rows = [
        {"ts": "2026-06-01T00:00:00Z", "mode": "live", "ok": True, "cost_usd": 0.02},
        {"ts": "2026-06-02T00:00:00Z", "mode": "live", "ok": True, "cost_usd": 0.04},
        {"ts": "2026-06-02T00:00:00Z", "mode": "mock", "ok": True, "cost_usd": 0.0},   # mock не считаем
        {"ts": "2026-06-02T00:00:00Z", "mode": "live", "ok": False, "cost_usd": None}, # неуспех не считаем
        {"ts": "2026-05-30T00:00:00Z", "mode": "live", "ok": True, "cost_usd": 9.0},   # прошлый месяц
    ]
    log = _costs_file(tmp_path, rows)
    est = RB.estimate_run_cost("masked_smoke", limits=_limits(), costs_log=log, month="2026-06")
    assert est["basis_avg"] == "история"
    assert est["avg_call_usd"] == pytest.approx(0.03)        # (0.02+0.04)/2
    assert est["estimate_usd"] == pytest.approx(5 * 0.03)


# ── Пред-проверка: разрешение и отказы ──────────────────────────────────────────
def test_precheck_allows_within_budget(tmp_path):
    log = _costs_file(tmp_path, [])
    d = RB.precheck("masked_smoke", limits=_limits(), costs_log=log, month="2026-06")
    assert d["allowed"] is True
    assert d["cap_usd"] == 3.0


def test_precheck_refuses_when_estimate_exceeds_mode_cap(tmp_path):
    # дорогие вызовы в истории → оценка прогона > потолка режима masked_smoke ($3)
    rows = [{"ts": "2026-06-01T00:00:00Z", "mode": "live", "ok": True, "cost_usd": 1.0}]
    log = _costs_file(tmp_path, rows)
    d = RB.precheck("masked_smoke", limits=_limits(), costs_log=log, month="2026-06")
    assert d["allowed"] is False
    assert d["контур"] == "потолок_режима"
    assert "ОТКАЗ" in d["reason"]


def test_precheck_refuses_when_month_cap_already_reached(tmp_path):
    # месячный спенд уже ≥ потолка 700 → стоп всех прогонов (§30 п.2)
    rows = [{"ts": "2026-06-01T00:00:00Z", "mode": "live", "ok": True, "cost_usd": 700.0}]
    log = _costs_file(tmp_path, rows)
    d = RB.precheck("masked_smoke", limits=_limits(), costs_log=log, month="2026-06")
    assert d["allowed"] is False
    assert d["контур"] == "месячный_потолок"


def test_precheck_or_raise(tmp_path):
    rows = [{"ts": "2026-06-01T00:00:00Z", "mode": "live", "ok": True, "cost_usd": 1.0}]
    log = _costs_file(tmp_path, rows)
    with pytest.raises(RB.RunBudgetRefused):
        RB.precheck_or_raise("masked_smoke", limits=_limits(), costs_log=log, month="2026-06")


# ── Стоп на лету ─────────────────────────────────────────────────────────────────
def test_guard_raises_on_overrun():
    g = RB.RunBudgetGuard("masked_smoke", cap_usd=0.10)
    g.add(0.04)
    g.add(0.04)            # 0.08 < 0.10 — ок
    with pytest.raises(RB.RunBudgetExceeded):
        g.add(0.05)        # 0.13 ≥ 0.10 — стоп
    assert g.calls == 3


def test_guard_charges_none_cost_with_estimate():
    # Кросс-ревью ночи 04.07: None-вызовы НЕ бесплатны для стопа — начисляется скользящее
    # среднее известных стоимостей (до первой известной — консервативный прайор).
    g = RB.RunBudgetGuard("event_first", 100.0)
    g.add(None)
    assert g.spent_usd == g.PRIOR_CALL_USD              # прайор до первой известной стоимости
    g2 = RB.RunBudgetGuard("event_first", 100.0)
    g2.add(2.0)
    g2.add(None)                                        # оценка = среднее известных = 2.0
    assert g2.spent_usd == 4.0 and g2.estimated_usd == 2.0 and g2.unaccounted_calls == 1


def test_unaccounted_calls_visible_not_free():
    # M7 + кросс-ревью ночи: вызов без стоимости считается, платен оценкой и рвёт cap.
    import pytest
    g = RB.RunBudgetGuard("event_first", 10.0)
    g.add(1.0)
    g.add(None)
    assert g.calls == 2 and g.unaccounted_calls == 1
    assert g.spent_usd == 2.0                           # 1.0 известная + 1.0 оценка (среднее)
    tight = RB.RunBudgetGuard("event_first", 0.3)
    with pytest.raises(RB.RunBudgetExceeded):
        for _ in range(10):
            tight.add(None)                             # cap рвётся и на None-вызовах
