# -*- coding: utf-8 -*-
"""Устойчивость call_agent к битым ответам модели (регрессия Нед.8: пустой ответ судьи ронял
весь 13-кейсовый masked-прогон на resp['text'][:600] при text=None).

Требование: одиночный пустой/None ответ модели → запись ok=False, НЕ исключение наружу
(прогон продолжается; Дирижёр видит брак и обрабатывает как ВЕТО/нет-данных)."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import agents as A      # noqa: E402
from orchestrator import openrouter as OR  # noqa: E402


class _StubClient(OR.BaseClient):
    """Клиент, возвращающий заданный text (в т.ч. None) — для проверки обработки брака."""
    mode = "stub"

    def __init__(self, text):
        super().__init__()
        self._text = text

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        return {"text": self._text, "model": "stub/model", "usage": {}, "cost": 0.0,
                "fallback_index": 0}


def test_none_text_does_not_crash():
    rec = A.call_agent("e_judge", {}, _StubClient(None), user_prompt="дело")
    assert rec["ok"] is False
    assert rec["stage"] == "parse"
    assert rec["raw"] == ""            # не падает на None[:600]


def test_empty_text_does_not_crash():
    rec = A.call_agent("e_judge", {}, _StubClient("   "), user_prompt="дело")
    assert rec["ok"] is False
    assert rec["stage"] == "parse"


def test_garbage_text_recorded_not_raised():
    rec = A.call_agent("e_generator", {}, _StubClient("не json вовсе"), user_prompt="дело")
    assert rec["ok"] is False
    assert "raw" in rec and isinstance(rec["raw"], str)
