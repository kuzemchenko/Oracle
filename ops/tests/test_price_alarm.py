# -*- coding: utf-8 -*-
"""Q-ценовой-будильник (испытатель 24.11): «поставь триггер на цену — сразу напиши, я куплю»."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import bot_watchlist as W    # noqa: E402
import bot_reports as R      # noqa: E402
import bot_agent as A        # noqa: E402
import bot as BOT            # noqa: E402
import bot_state as S        # noqa: E402
from test_bot_v2_loop import FakeTelegram  # noqa: E402

CUR = {"date": "2026-11-18", "close": 156.14}


def _add(tmp_path, **kw):
    args = dict(ticker="NVDA", level=140, direction="below", today="2026-11-18", current=CUR,
                condition="не покупать, если упало из-за проверки бухгалтерии", note="эксперимент на $100",
                path=tmp_path / "wl.jsonl")
    args.update(kw)
    return W.add_price_alarm(**{k: v for k, v in args.items() if k not in ("ticker", "level", "direction")},
                             ticker=args["ticker"], level=args["level"], direction=args["direction"])


def test_alarm_rejects_level_already_passed_and_bad_input(tmp_path):
    assert "уже ниже" in _add(tmp_path, level=160)[1]
    assert "уже выше" in _add(tmp_path, level=150, direction="above")[1]
    assert "direction" in _add(tmp_path, direction="down")[1]
    assert "нет свежей цены" in _add(tmp_path, current=None)[1]
    assert "срок" in _add(tmp_path, until="2028-01-01")[1]
    assert "слишком далеко" in _add(tmp_path, level=10)[1]


def test_alarm_fires_on_close_after_creation_once(tmp_path):
    rec, why = _add(tmp_path)
    assert why is None and rec["trigger"]["symbol"] == "NVDA.US" and rec["condition"]
    assert "уже стоит" in _add(tmp_path)[1]
    closes = {"NVDA.US": [("2026-11-18", 139.0), ("2026-11-19", 145.0), ("2026-11-20", 128.76)]}
    fn = lambda sym, since: closes.get(sym, [])                       # noqa: E731
    fired, expired = W.evaluate_alarms(fn, path=tmp_path / "wl.jsonl", today="2026-11-21")
    assert len(fired) == 1 and fired[0]["observed"] == {"date": "2026-11-20", "close": 128.76}  # 18.11 — день постановки
    assert expired == []
    W.fire_entry(fired[0]["entry"], fired[0]["observed"], path=tmp_path / "wl.jsonl")
    assert W.evaluate_alarms(fn, path=tmp_path / "wl.jsonl", today="2026-11-22") == ([], [])


def test_alarm_expires_without_hit(tmp_path):
    _add(tmp_path, until="2026-11-25")
    fired, expired = W.evaluate_alarms(lambda s, d: [("2026-11-19", 150.0)], path=tmp_path / "wl.jsonl",
                                       today="2026-11-26")
    assert fired == [] and len(expired) == 1


def test_old_structural_evaluate_ignores_alarms(tmp_path, monkeypatch):
    _add(tmp_path)
    monkeypatch.setattr(W, "latest_close", lambda *a, **k: {"date": "2026-11-20", "close": 100.0})
    assert W.evaluate(path=tmp_path / "wl.jsonl") == []


def test_alarm_message_text():
    rec = {"asset": "NVDA.US", "note": "эксперимент на $100", "condition": "не покупать, если упало из-за бухгалтерии",
           "trigger": {"level": 140.0, "dir": "below", "anchor": {"date": "2026-11-18", "close": 156.14}}}
    t = R.format_price_alarm({"entry": rec, "observed": {"date": "2026-11-20", "close": 128.76}})
    assert "закрылась ниже $140" in t and "$128.76" in t and "$156.14" in t
    assert "проверь его до покупки" in t and "Решение и риск — твои" in t


def test_agent_tool_hidden_and_refuses_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_flag", lambda n: False)
    assert "price_alarm_set" not in {t["function"]["name"] for t in A.tool_specs()}
    tools = A.Tools()
    assert "не включён владельцем" in tools.price_alarm_set("NVDA", 140, "below")


def test_agent_tool_sets_alarm_and_code_writes_line(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_flag", lambda n: True)
    tools = A.Tools()
    tools.watchlist_path = tmp_path / "wl.jsonl"
    monkeypatch.setattr(tools, "_last_close", lambda sym: CUR)
    out = tools.price_alarm_set("NVDA", 140, "below", condition="не покупать, если упало из-за бухгалтерии")
    assert out.startswith("поставлен") and tools.sealed and "закроется ниже $140" in tools.sealed[-1]
    assert "NVDA.US" in tools.price_alarm_list()
    eid = json.loads(tools.price_alarm_list())[0]["id"]
    assert "только по словам владельца" in tools.price_alarm_cancel(eid)
    assert "снят" in tools.price_alarm_cancel(eid, owner_words="сними будильник по Nvidia")
    assert tools.price_alarm_list() == "активных будильников нет"


def test_bot_sends_once_per_hit_behind_flag(tmp_path, monkeypatch):
    _add(tmp_path, path=W.WATCHLIST_PATH)
    tg = FakeTelegram()
    b = BOT.Bot(tg, "555", state=S._default_state())
    monkeypatch.setattr(b, "_alarm_closes", lambda sym, since: [("2026-11-20", 128.76)])
    now = datetime.datetime(2026, 11, 21, 9, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(R, "load_presentation", lambda: {"price_alarm_push": False})
    b._tick_price_alarms(now)
    assert tg.sent == []
    monkeypatch.setattr(R, "load_presentation", lambda: {"price_alarm_push": True})
    b._tick_price_alarms(now)
    assert len(tg.sent) == 1 and "Будильник" in tg.sent[0][1]
    b._tick_price_alarms(now + datetime.timedelta(hours=2))
    assert len(tg.sent) == 1                                          # не повторяется
