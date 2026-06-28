# -*- coding: utf-8 -*-
"""Тесты интроспекции чата-Дирижёра (ops/bot_introspect.py, R9b).

Гермётично: роутер relevant_fact и извлечение тикера — без БД/сети (leaf-функции монкипатчатся).
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import bot_introspect as I    # noqa: E402


def test_ticker_extract():
    assert I._ticker("дай borrow по CLF") == "CLF.US"
    assert I._ticker("почему LNG в топе") == "LNG.US"
    assert I._ticker("какой brier сейчас") is None          # нет тикер-токена


def test_relevant_fact_routes(monkeypatch):
    monkeypatch.setattr(I, "brier_tracks", lambda: "BRIER")
    monkeypatch.setattr(I, "borrow", lambda s: f"BORROW:{s}")
    monkeypatch.setattr(I, "sealed_summary", lambda: "SEALED")
    monkeypatch.setattr(I, "why_node", lambda s: f"WHY:{s}")
    monkeypatch.setattr(I, "latest_graph", lambda: "GRAPH")
    assert I.relevant_fact("какой сейчас brier по трекам?") == "BRIER"
    assert I.relevant_fact("дай borrow по CLF") == "BORROW:CLF.US"
    assert I.relevant_fact("сколько всего запечатано?") == "SEALED"
    assert I.relevant_fact("почему LNG в топе?") == "WHY:LNG.US"
    assert I.relevant_fact("покажи граф последнего прогона") == "GRAPH"
    assert I.relevant_fact("спасибо, понятно") is None      # свободный диалог, фактовой привязки нет
