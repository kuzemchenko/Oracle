# -*- coding: utf-8 -*-
"""Разбор 31.10.2026: источники данных умирали молча (GDELT 0 статей с 26.10, Trends-ключи реестра
3,5 месяца без фетча) — заметки о здоровье источников владельцу."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data"))
import news_ingest as NI  # noqa: E402


def test_healthy_sources_give_no_notice():
    assert NI.source_health_notices((1500, 1700, 3), (1674, 500, 0, 57)) == []


def test_gdelt_zero_day_is_reported():
    out = NI.source_health_notices((0, 0, 21), None)
    assert len(out) == 1 and "GDELT" in out[0] and "429" in out[0]


def test_gdelt_quiet_day_without_failures_is_not_an_alarm():
    # выходной без новых статей, запросы прошли — не поломка
    assert NI.source_health_notices((0, 0, 0), None) == []


def test_trends_all_rate_limited_and_half_rate_limited():
    full = NI.source_health_notices(None, (0, 0, 57, 57))
    half = NI.source_health_notices(None, (1674, 500, 38, 57))
    fine = NI.source_health_notices(None, (1674, 500, 5, 57))
    assert len(full) == 1 and "все 57" in full[0]
    assert len(half) == 1 and "38 из 57" in half[0]
    assert fine == []


def test_notice_appends_json_line(tmp_path):
    p = tmp_path / "notices.jsonl"
    NI._notice("проверка", path=p)
    NI._notice("вторая", path=p)
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]
    assert [r["text"] for r in rows] == ["проверка", "вторая"] and all("ts" in r for r in rows)
