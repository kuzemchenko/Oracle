# -*- coding: utf-8 -*-
"""VQ-4: цена вызова по режиму (разговоры владельца не раздувают оценку прогона) + урезание плана."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator import run_budget as RB   # noqa: E402
from orchestrator import event_first as EF  # noqa: E402

LIM = {"budget": {"total_usd_month": 700, "costs_log": "x"},
       "per_run_token_budget_usd": {"event_first": 10.0}, "per_run_expected_calls": {"event_first": 120},
       "cost_per_call_prior_usd": 0.04}


def _log(tmp_path, rows):
    p = tmp_path / "costs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _row(rid, cost, ts="2026-11-08T09:00:00Z"):
    return {"ts": ts, "mode": "live", "ok": True, "cost_usd": cost, "run_id": rid}


def test_owner_chat_does_not_inflate_morning_run(tmp_path):
    # 08.11: 363 вызова прогона по $0.06 + 123 разговора по $0.23 → по старой средней 120×$0.10 > $10
    rows = [_row(f"ef_20261107T090001Z__{i % 3}", 0.06) for i in range(363)] + \
           [_row("bot_agent", 0.23) for _ in range(123)]
    log = _log(tmp_path, rows)
    avg, basis, n = RB.avg_call_cost(log, month="2026-11", mode="event_first")
    assert basis == "режим" and n == 363 and abs(avg - 0.06) < 1e-9
    est = RB.estimate_run_cost("event_first", limits=LIM, costs_log=log, month="2026-11")
    assert est["expected_calls"] == 120                     # план вызовов НЕ уменьшен
    assert abs(est["estimate_usd"] - 7.2) < 1e-6 and est["estimate_usd"] < 10
    assert est["факт_прогонов"]["n"] == 1                   # подпрогоны run__src склеены в один прогон


def test_thin_mode_history_falls_back_to_background_without_chat(tmp_path):
    rows = [_row("wo_1", 0.12)] * 3 + [_row("bot_agent", 1.0)] * 50 + [_row("ef_x", 0.05)] * 5
    avg, basis, _ = RB.avg_call_cost(_log(tmp_path, rows), month="2026-11", mode="week_odds")
    assert basis == "история" and avg < 0.1                 # разговоры по $1 не учтены
    avg2, basis2, _ = RB.avg_call_cost(tmp_path / "нет.jsonl", month="2026-11", prior=0.04)
    assert basis2 == "приор" and avg2 == 0.04


def test_fit_plan_cuts_optional_keeps_mandatory():
    stages = [{"этап": "картограф", "вызовов": 24, "обязательный": False},
              {"этап": "суд", "вызовов": 96, "обязательный": True}]
    fit = RB.fit_plan(stages, cap_usd=10.0, avg_call_usd=0.1)       # влезает 100 вызовов
    assert fit["влезает"] and fit["вызовов"] == 100 and fit["отложено"] == {"картограф": 20}
    assert next(s for s in fit["этапы"] if s["этап"] == "суд")["разрешено"] == 96
    assert not RB.fit_plan(stages, cap_usd=5.0, avg_call_usd=0.1)["влезает"]   # суд не влезает → отказ


def test_event_first_degrades_instead_of_refusing():
    dec = {"expected_calls": 120, "cap_usd": 10.0, "avg_call_usd": 0.095}  # 105 вызовов влезает
    u = EF._fit_event_first(dec)
    assert u and u["карт_разрешено"] == 9 and u["карт_отложено"] == 15 and u["вызовов"] == 105
    assert EF._fit_event_first({"expected_calls": 120, "cap_usd": 10.0, "avg_call_usd": 0.12}) is None
