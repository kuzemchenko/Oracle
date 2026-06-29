# -*- coding: utf-8 -*-
"""Долг[HIGH] из stage-review F0: хард-стоп бюджета (§24) НЕ должен глотаться широкими except.

RunBudgetExceeded наследует BaseException → фолбэк-циклы openrouter.complete и обработчики ошибок
agents.call_agent/_vet_money его НЕ перехватывают; сигнал стопа долетает до входа прогона.
"""
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import run_budget as RB        # noqa: E402
from orchestrator import openrouter as OR        # noqa: E402


def test_exceeded_is_baseexception_not_exception():
    # ключевое свойство: НЕ ловится `except Exception` (как KeyboardInterrupt/SystemExit)
    assert issubclass(RB.RunBudgetExceeded, BaseException)
    assert not issubclass(RB.RunBudgetExceeded, Exception)


def test_guard_add_not_swallowed_by_except_exception():
    guard = RB.RunBudgetGuard("test", cap_usd=1.0)
    swallowed = False
    with pytest.raises(RB.RunBudgetExceeded):
        try:
            guard.add(2.0)              # 2.0 ≥ 1.0 → стоп
        except Exception:               # noqa: BLE001 — ИМЕННО это глотало сигнал до фикса
            swallowed = True
    assert swallowed is False


def test_complete_propagates_hardstop_not_fallback(monkeypatch):
    # реальный путь LiveClient.complete: успешный вызов → cost_guard.add рвёт → НЕ уходит в фолбэк
    client = OR.LiveClient(models=OR.load_models(), run_id="pytest_hardstop", api_key="x")
    monkeypatch.setattr(client, "_one_call",
                        lambda *a, **k: ("текст", {"total_tokens": 10}, 5.0))   # cost=5.0
    monkeypatch.setattr(OR, "log_cost", lambda *a, **k: None)                   # не писать журнал трат
    client.cost_guard = RB.RunBudgetGuard("event_first", cap_usd=1.0)           # потолок 1.0 < 5.0
    role = OR.load_models().get("roles") and "generator"
    with pytest.raises(RB.RunBudgetExceeded):
        client.complete("generator", "sys", "usr", agent_id="a_test", output_kind="judgment")


def test_run_funnel_returns_stop_protocol_on_hardstop(monkeypatch):
    # обёртка run_funnel ловит хард-стоп и отдаёт протокол-стоп (не крэш)
    from orchestrator import funnel as F
    monkeypatch.setattr(F, "_run_funnel",
                        lambda **k: (_ for _ in ()).throw(RB.RunBudgetExceeded("funnel_full", 9.0, 8.0)))
    p = F.run_funnel(theme="brent", mode="mock", run_id="pytest_stop", write=False)
    assert "ОСТАНОВ_бюджет" in p
    assert p["ОСТАНОВ_бюджет"]["cap_usd"] == 8.0
