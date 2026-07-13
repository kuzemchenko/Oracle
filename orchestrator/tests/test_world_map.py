# -*- coding: utf-8 -*-
"""Тесты Э4(а) «перебор мира» — LLM-карта сегментов (orchestrator/world_map.py).
Только MockClient/фейк-клиенты (LIVE LLM в разработке запрещён — рамки Э4)."""
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import world_map as WM               # noqa: E402
from orchestrator.openrouter import MockClient          # noqa: E402
from orchestrator import run_budget as RB               # noqa: E402

EVENT = {"событие": "тестовое событие", "ключи": ["a", "b"],
         "источник_шока": "AAA.US", "дата": "2026-07-13"}


class FakeClient:
    """Фейк-клиент с фиксированным текстом ответа (фикстурный путь)."""
    mode = "mock"

    def __init__(self, text):
        self.text = text
        self.cost_guard = None
        self.calls = 0

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        self.calls += 1
        if self.cost_guard is not None:
            self.cost_guard.add(0.0)
        return {"text": self.text, "model": "fake/model", "usage": {}, "cost": 0.0}


def _valid_doc():
    return {"событие": "e", "сегменты": [
        {"сегмент": "Электрооборудование", "порядок": 2, "направление": "рост",
         "канал": "capex", "механизм": "спрос на сети", "секторы": ["Industrials"],
         "индустрии": ["Electrical Equipment & Parts"]}],
        "обоснование": "x", "уверенность": "средняя"}


def test_mockclient_map_valid_and_provenance():
    env = WM.build_world_map(EVENT, MockClient(), run_id="t1")
    assert env["отказ"] is None
    assert env["карта"]["сегменты"], "MockClient обязан давать валидную карту"
    for seg in env["карта"]["сегменты"]:
        assert 1 <= seg["порядок"] <= 4 and seg["направление"] in WM.DIRECTIONS
        assert seg["секторы"] and all(s in WM.EODHD_SECTORS for s in seg["секторы"])
    assert env["провенанс"]["модель"] and env["провенанс"]["run_id"] == "t1"
    assert env["ttl_days"] > 0                     # срок жизни ставит КОД, не LLM


def test_garbage_answer_honest_refusal():
    env = WM.build_world_map(EVENT, FakeClient("ничего не знаю, вот вам стихи"), run_id="t")
    assert env["карта"] is None and "не парсится" in env["отказ"]


def test_empty_segments_is_event_refusal_p8():
    doc = {"событие": "e", "сегменты": [], "обоснование": "переноса нет"}
    env = WM.build_world_map(EVENT, FakeClient(json.dumps(doc, ensure_ascii=False)))
    assert env["карта"] is None and "карта пуста" in env["отказ"]
    assert "переноса нет" in env["отказ"]           # причина LLM дословно (П8)


def test_validation_rejects_llm_numbers_and_tickers():
    doc = _valid_doc()
    doc["сегменты"].append({"сегмент": "NUE", "порядок": 3, "направление": "рост",
                            "механизм": "m", "секторы": ["Basic Materials"]})   # тикер-имя
    doc["сегменты"].append({"сегмент": "Сталь", "порядок": 3, "направление": "рост",
                            "механизм": "m", "секторы": ["Basic Materials"],
                            "вероятность": 0.8})                                # LLM-величина (рамка 2)
    doc["сегменты"].append({"сегмент": "Медь", "порядок": 3, "направление": "рост",
                            "механизм": "m", "секторы": ["Basic Materials"],
                            "тикеры": ["FCX.US"]})                              # тикеры запрещены
    карта, problems = WM.validate_map(doc)
    assert len(карта["сегменты"]) == 1              # выжил только валидный
    txt = "; ".join(problems)
    assert "похоже на тикер" in txt and "LLM-величины запрещены" in txt and "запрещено" in txt


def test_validation_rejects_bad_order_direction_sector():
    doc = {"сегменты": [
        {"сегмент": "X", "порядок": 5, "направление": "рост", "механизм": "m",
         "секторы": ["Industrials"]},
        {"сегмент": "Y", "порядок": 2, "направление": "вбок", "механизм": "m",
         "секторы": ["Industrials"]},
        {"сегмент": "Z", "порядок": 2, "направление": "рост", "механизм": "m",
         "секторы": ["Носки"]}]}
    карта, problems = WM.validate_map(doc)
    assert карта is None and "ни одного валидного сегмента" in problems[-1]


def test_llm_exception_fail_soft_refusal():
    class Boom(FakeClient):
        def complete(self, *a, **k):
            raise RuntimeError("все модели роли исчерпаны")
    env = WM.build_world_map(EVENT, Boom(""), run_id="t")
    assert env["карта"] is None and "сбой LLM-картографа мира" in env["отказ"]


def test_budget_exceeded_not_swallowed():
    """RunBudgetExceeded — BaseException: стоп §24 обязан долететь, не превратиться в отказ."""
    class Expensive(FakeClient):
        def complete(self, *a, **k):
            self.cost_guard.add(99.0)
            return {"text": "{}", "model": "m", "usage": {}, "cost": 99.0}
    c = Expensive("")
    c.cost_guard = RB.RunBudgetGuard("world_map", 3.0)
    with pytest.raises(BaseException) as ei:
        WM.build_world_map(EVENT, c)
    assert isinstance(ei.value, RB.RunBudgetExceeded)


def test_ttl_from_limits_config():
    assert WM.map_ttl_days({"world_enum": {"map_ttl_days": 7}}) == 7
    assert WM.map_ttl_days({}) == 28                # фолбэк не fail-open (константа кода)
