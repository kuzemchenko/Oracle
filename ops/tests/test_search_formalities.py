# -*- coding: utf-8 -*-
"""Q-поиск-формальности (испытатель 17.11): поиск 16.11 сжёг $17 на спор модели с формой кода."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ops import search_queue as SQ      # noqa: E402
from ops import evidence_check as EC    # noqa: E402
import bot_agent as A                   # noqa: E402
from test_search_queue import worthy_data, _msg, FakeLLM  # noqa: E402


def _at_evaluation(tmp_path, data):
    s = SQ.start("танкеры", "telegram", d=tmp_path)
    h, _ = SQ.add_hypothesis(s, "Спотовые супертанкеры выигрывают от страховки военного риска в Заливе", "FRO",
                             "дорожает страховка — часть судов не идёт в Залив — ставки фрахта растут")
    for _ in range(4):
        SQ.advance(s, h["id"], "pass", data)
    assert h["этап"] == "оценка"
    return s, h


def test_unknown_kind_gets_nearest_value_hint():
    kind, hint = EC.normalize_market_kind("options")
    assert kind is None and "скорее всего нужен 'magnitude'" in hint and "timing — срок" in hint
    assert EC.normalize_market_kind("Timing") == ("timing", "")
    assert EC.normalize_market_kind("масштаб") == ("magnitude", "")
    assert EC.normalize_market_kind(None) == ("magnitude", "")
    assert "скорее всего" not in EC.normalize_market_kind("zzz")[1]


def test_russian_synonym_passes_evaluation(tmp_path):
    d = worthy_data()
    d["market_view"]["kind"] = "Масштаб"
    s, h = _at_evaluation(tmp_path, d)
    _h, msg = SQ.advance(s, h["id"], "pass")
    assert "пройден" in msg, msg


def test_partial_fix_of_one_field_is_merged(tmp_path):
    d = worthy_data()
    d["market_view"]["kind"] = "options"
    s, h = _at_evaluation(tmp_path, d)
    _h, msg = SQ.advance(s, h["id"], "pass")
    assert "скорее всего нужен 'magnitude'" in msg and "Пришли только недостающие поля" in msg
    _h, msg = SQ.advance(s, h["id"], "pass", {"market_view": {"kind": "magnitude"}})   # только одно поле
    assert "пройден" in msg, msg
    assert h["данные"]["market_view"]["why_different"]           # остальное не затёрто


def test_same_gap_twice_says_dont_repeat_third_time_code_sets_needs_data(tmp_path):
    d = worthy_data()
    d["market_view"]["kind"] = "options"
    s, h = _at_evaluation(tmp_path, d)
    m1 = SQ.advance(s, h["id"], "pass")[1]
    m2 = SQ.advance(s, h["id"], "pass", {"market_view": {"kind": "options"}})[1]
    assert "ТА ЖЕ нехватка второй раз" in m2 and "ТА ЖЕ" not in m1
    m3 = SQ.advance(s, h["id"], "pass")[1]
    assert "код сам поставил" in m3 and h["вердикт"] == SQ.NEEDS_DATA and h["нужно_данных"]
    assert s["статус"] == SQ.RUNNING                               # поиск идёт к другим направлениям


def test_different_gap_resets_counter(tmp_path):
    d = worthy_data()
    d["market_view"]["kind"] = "options"
    s, h = _at_evaluation(tmp_path, d)
    SQ.advance(s, h["id"], "pass")
    SQ.advance(s, h["id"], "pass")
    SQ.advance(s, h["id"], "pass", {"market_view": {"kind": "magnitude"}, "критика": {"семейство": "", "возражения": []}})
    assert h["вердикт"] != SQ.NEEDS_DATA


def test_broken_json_args_do_not_call_tool_and_say_why(tmp_path):
    tools = A.Tools()
    tools.search_dir = tmp_path
    called = []
    orig = tools.call
    tools.call = lambda n, a: called.append(n) or orig(n, a)
    bad = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c0", "type": "function", "function": {"name": "search_step",
                                                       "arguments": '{"hyp_id": "h2", "outcome": "pass", "data": {"ma'}}]},
        "finish_reason": "tool_calls"}], "usage": {"cost": 0.01}}
    llm = FakeLLM([bad, _msg("ок")])
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag.run("проверь")
    assert called == []
    tool_msgs = [m for m in llm.bodies[-1]["messages"] if m.get("role") == "tool"]
    c = json.dumps(tool_msgs[0]["content"], ensure_ascii=False)
    assert "не разобраны: JSON оборван" in c and "Инструмент НЕ вызван" in c


def test_tool_description_lists_kinds():
    spec = next(t for t in A.TOOL_SPECS if t["function"]["name"] == "search_step")
    d = json.dumps(spec, ensure_ascii=False)
    assert "magnitude — размер эффекта" in d and "частями" in d


def test_turn_stops_at_remaining_search_budget_not_full_turn_cap(tmp_path):
    """Машинист 20.11: $22.26 при потолке $20 — ответ у самого потолка проходил его целиком."""
    tools = A.Tools()
    tools.search_dir = tmp_path
    s = SQ.start("дроны", "telegram", d=tmp_path)
    s["бюджет"]["лимит_usd"], s["бюджет"]["потрачено_usd"] = 20.0, 19.80
    SQ.save(s, tmp_path)
    tools.search_id = s["id"]
    script = [_msg(calls=[("search_step", {"hyp_id": "h9", "outcome": "pass"})], cost=0.10) for _ in range(30)]
    llm = FakeLLM(script)
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    _text, _f, cost = ag.run("продолжай поиск")
    assert cost <= 0.40, cost                       # было бы до $2.50+
    s = SQ.load(s["id"], tmp_path)
    assert s["статус"] == SQ.PAUSED and s["пауза"]["вид"] == "budget"
    assert any("бюджет поиска исчерпан" in str(m.get("content")) for b in llm.bodies for m in b["messages"]
               if m.get("role") == "user")
