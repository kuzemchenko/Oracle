# -*- coding: utf-8 -*-
"""Суд разных семейств над идеей охотника (ops/hunter_court.py, П10, ревью 02.11).

Закрепляют: критик вызывается с исключением семейства генератора (anthropic); судья — с
исключением И генератора, И фактического семейства критика; FATAL без улики = FIX (суд не
казнит без доказательства); мусорный ответ — не голос (суд не состоялся → None, бот не шлёт);
нет живого клиента — честный отказ; строка второй проверки — без приговора."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import hunter_court as HC          # noqa: E402
from test_idea_gate import CARD    # noqa: E402


class Stub:
    is_stub = True
    mode = "stub"

    def __init__(self, crit, judge):
        self.crit, self.judge, self.calls = crit, judge, []

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        self.calls.append((role, set(exclude_family or ())))
        if role == "critic":
            return {"text": self.crit, "model": "openai/gpt-5.5"}
        return {"text": self.judge, "model": "google/gemini-3.1-pro-preview"}


def test_families_excluded_and_verdict_parsed():
    st = Stub('{"verdict":"FIX","point":2,"evidence":"","objection":"канал к выручке не показан"}',
              'ответ: {"verdict":"PASS","point":0,"evidence":"","objection":"спрос","answer":"контракт"}')
    v, why = HC.judge_card(CARD, client=st)
    assert why is None and v["verdict"] == "PASS" and v["critic"]["verdict"] == "FIX"
    assert st.calls[0] == ("critic", {"anthropic"})
    assert st.calls[1] == ("judge", {"anthropic", "openai"})


def test_fatal_without_evidence_is_fix_and_garbage_is_no_vote():
    st = Stub('{"verdict":"FATAL","evidence":"","objection":"x"}', '{"verdict":"FATAL","evidence":"","objection":"y"}')
    v, _ = HC.judge_card(CARD, client=st)
    assert v["verdict"] == "FIX"
    v2, why2 = HC.judge_card(CARD, client=Stub("не JSON", "{}"))
    assert v2 is None and "невалидный" in why2


def test_no_live_client_is_honest_refusal():
    class Mock:
        mode = "mock"
    v, why = HC.judge_card(CARD, client=Mock())
    assert v is None and "недоступен" in why


def test_second_check_line_no_verdict_words():
    line = HC.second_check_line({"objection": "спрос может не подтвердиться."})
    assert "Вторая проверка" in line and "FATAL" not in line and "FIX" not in line
    assert HC.second_check_line({"objection": ""}) is None
