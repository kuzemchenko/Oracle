# -*- coding: utf-8 -*-
"""Тесты панели бюджета (§11, §12, §30, инвариант 5).

Закрепляют РЕШЕНИЕ Недели 1: источник правды спенда «Оракула» — journal/costs.jsonl;
цифра ключа /api/v1/key — только справка БЕЗ алертов; потолки — из limits.yaml, не хардкод.
"""
import sys
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
import budget as B  # noqa: E402

LIMITS = {"tokens_usd_month": 500.0, "data_usd_month": 200.0, "total_usd_month": 700.0,
          "alert_fraction": 0.8, "costs_log": None}


def _write_costs(tmp_path, rows):
    p = tmp_path / "costs.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return p


def test_limits_read_from_yaml_not_hardcoded():
    lim = B.load_budget_limits()
    # значения берутся из config/limits.yaml (budget.*), а не из констант
    assert lim["tokens_usd_month"] == 500.0
    assert lim["data_usd_month"] == 200.0
    assert lim["total_usd_month"] == 700.0
    assert lim["alert_fraction"] == 0.8
    assert str(lim["costs_log"]).endswith("journal/costs.jsonl")


def test_oracle_spend_sums_journal_excludes_mock(tmp_path):
    rows = [
        {"ts": "2026-06-01T00:00:00Z", "mode": "live", "model": "anthropic/x", "cost_usd": 0.50},
        {"ts": "2026-06-02T00:00:00Z", "mode": "live", "model": "openai/y", "cost_usd": 0.30},
        {"ts": "2026-06-03T00:00:00Z", "mode": "mock", "model": "z", "cost_usd": 0.0},   # mock — игнор
        {"ts": "2026-05-30T00:00:00Z", "mode": "live", "model": "q", "cost_usd": 9.99},  # прошлый месяц
    ]
    p = _write_costs(tmp_path, rows)
    total, by_mode, by_model = B.oracle_monthly_spend(p, month="2026-06")
    assert total == pytest.approx(0.80)
    assert "mock" not in by_mode
    assert set(by_model) == {"anthropic/x", "openai/y"}


def test_alerts_only_on_oracle_spend_not_key():
    # Спенд «Оракула» мал → OK, даже если ключ в реальности почти выработан.
    st = B.compute_status(0.82, LIMITS)
    assert st["status"] == "OK"
    assert st["exit_code"] == 0
    assert st["tokens_frac"] < 0.01


def test_warning_at_80pct_oracle_tokens():
    st = B.compute_status(0.80 * 500, LIMITS)  # ровно 80% токенов
    assert "ВНИМАНИЕ" in st["status"]
    assert st["exit_code"] == 0


def test_stop_at_100pct_oracle_tokens():
    st = B.compute_status(500.0, LIMITS)  # 100% токенов
    assert "ПРЕВЫШЕНИЕ" in st["status"]
    assert st["exit_code"] == 3


def test_total_cap_can_trigger_alert():
    # токены ниже порога, но всего (токены+данные200) пробивает total → алерт
    st = B.compute_status(360.0, LIMITS)  # 360+200=560/700=0.8
    assert "ВНИМАНИЕ" in st["status"]


def test_one_liner_separates_oracle_key_and_daily_remaining():
    st = B.compute_status(0.82, LIMITS)
    key = {"usage_monthly": 459.97, "limit_remaining": 237.47, "limit": 300, "limit_reset": "daily"}
    line = B.one_liner(st, key)
    assert "Оракул токены $0.82/$500" in line          # (1) журнал
    assert "459.97/мес" in line                          # (2) справка ключ
    assert "237.47/$300" in line                         # (3) дневной остаток
    assert "без алертов" in line


def test_one_liner_handles_key_error():
    st = B.compute_status(0.82, LIMITS)
    line = B.one_liner(st, {"error": "OPENROUTER_API_KEY не задан"})
    assert "справка ключ: недоступна" in line
    assert st["status"] == "OK"  # ошибка справки НЕ влияет на статус «Оракула»
