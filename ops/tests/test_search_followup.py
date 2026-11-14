# -*- coding: utf-8 -*-
"""Доведение поиска ботом и сообщение о паузе (ревью 14.11: Codex «Г», Fable п.9)."""
import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import bot_state as S         # noqa: E402
import bot as BOT             # noqa: E402
import search_queue as SQ     # noqa: E402

NOON = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=datetime.timezone.utc)


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        return len(self.sent)


@pytest.fixture
def env(tmp_path, monkeypatch):
    flags = {"search_pause_notice": True, "search_followup_auto": True}
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "DELIVERIES_PATH", tmp_path / "deliveries.jsonl")
    monkeypatch.setattr(BOT.Bot, "SEARCH_DIR", tmp_path / "searches")
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: dict(flags))
    b = BOT.Bot(FakeTelegram(), "555", state=S._default_state())
    b.asked = []
    monkeypatch.setattr(b, "_chat", lambda chat, text, attachments=None: b.asked.append(text))
    return b, tmp_path / "searches", flags


def _search(d, kind="turn", hours_ago=3, spent=0.0):
    s = SQ.start("медь и стройка в Индии", "telegram", d=d)
    s["бюджет"]["потрачено_usd"] = spent
    SQ.pause(s, kind, "тест")
    SQ.save(s, d)
    s["обновлён"] = (NOON - datetime.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    (d / f"{s['id']}.json").write_text(__import__("json").dumps(s, ensure_ascii=False), encoding="utf-8")
    return s


def test_budget_pause_notified_once_with_what_to_do(env):
    b, d, _ = env
    _search(d, kind="budget", spent=10.0)
    b._tick_search_pause_notice()
    b._tick_search_pause_notice()
    assert len(b.tg.sent) == 1
    t = b.tg.sent[0][1]
    assert "остановлен" in t and "бюджет" in t and "подними бюджет" in t


def test_turn_pause_is_not_notified(env):
    b, d, _ = env
    _search(d, kind="turn")
    b._tick_search_pause_notice()
    assert not b.tg.sent


def test_followup_resumes_turn_pause_within_limits(env):
    b, d, _ = env
    s = _search(d, kind="turn", hours_ago=3)
    b._tick_search_followup(NOON)
    assert len(b.asked) == 1 and b.asked[0][0] == "__search__"
    assert "Поищи идею дальше" in b.asked[0][1] and "Продолжил поиск сам" in b.asked[0][2]
    assert SQ.load(s["id"], d)["статус"] == SQ.RUNNING


def test_followup_respects_gap_night_budget_flag_and_daily_cap(env, monkeypatch):
    b, d, flags = env
    _search(d, kind="turn", hours_ago=1)                           # двигали час назад — рано
    b._tick_search_followup(NOON)
    assert not b.asked
    night = datetime.datetime(2026, 11, 14, 19, 0, tzinfo=datetime.timezone.utc)   # 23:00 Дубай
    b._tick_search_followup(night)
    assert not b.asked
    flags["search_followup_auto"] = False
    b._tick_search_followup(NOON + datetime.timedelta(hours=5))
    assert not b.asked
    flags["search_followup_auto"] = True
    b.state["search_followup"] = {"день": NOON.date().isoformat(), "шагов": 3}
    b._tick_search_followup(NOON + datetime.timedelta(hours=5))
    assert not b.asked


def test_followup_skips_exhausted_budget(env):
    b, d, _ = env
    _search(d, kind="turn", hours_ago=5, spent=10.0)
    b._tick_search_followup(NOON)
    assert not b.asked


def test_flags_pass_real_loader():
    from ops import bot_reports as R
    cfg = R.load_presentation()
    assert "search_pause_notice" in cfg and "search_followup_auto" in cfg
