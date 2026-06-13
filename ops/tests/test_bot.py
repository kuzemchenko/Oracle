# -*- coding: utf-8 -*-
"""Тесты бота-пульта (раздел «Интерфейс»; П12, §12, §15, §17).

Закрепляют:
  • паузу пре-коммитмента §12 — «Принять» заблокирована < 24 ч от выдачи, открыта ≥ 24 ч;
  • append-only журнал решений §12 (принял/отклонил/отложил + мотив-аннотация);
  • извлечение РАНО-идей и срабатывание СТРУКТУРНОГО ценового триггера по oracle.db (§17);
  • allow-list по chat_id (кнопки решений недоступны посторонним);
  • пуш отчёта с дедупом и baseline против спама бэклогом.
Telegram замокан (FakeTelegram), сети нет.
"""
import sys
import json
import sqlite3
import datetime
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))

import bot_state as S        # noqa: E402
import bot_reports as R      # noqa: E402
import bot_watchlist as W    # noqa: E402
import bot as BOT            # noqa: E402

UTC = datetime.timezone.utc


# ── окружение: временные пути ───────────────────────────────────────────────────────
@pytest.fixture
def paths(tmp_path, monkeypatch):
    logs = tmp_path / "funnel_logs"
    logs.mkdir()
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    monkeypatch.setattr(W, "WATCHLIST_PATH", tmp_path / "watchlist.jsonl")
    monkeypatch.setattr(W, "DB_PATH", tmp_path / "oracle.db")
    monkeypatch.setattr(R, "FUNNEL_LOGS", logs)
    return {"tmp": tmp_path, "logs": logs,
            "decisions": tmp_path / "decisions_user.jsonl",
            "watchlist": tmp_path / "watchlist.jsonl",
            "db": tmp_path / "oracle.db"}


def _proto(run_id="funnel_20260612T140000Z", issued_at="2026-06-12T14:00:00+00:00",
           ideas=True, early=True):
    p = {"run_id": run_id, "ts": issued_at, "mode": "live", "theme": "brent",
         "этап3_грубый_фильтр": {"пер_кандидатные_вердикты": [
             {"актив": "Brent", "направление": "лонг", "тайминг": "ВОВРЕМЯ"}]},
         "воронка_отсева": {"этап2_кандидатов": 5, "этап5_устояло_после_дебатов": 0,
                            "этап6_выдано_топ": 0},
         "этап6_синтез": {"отчёты": []}}
    if early:
        p["этап3_грубый_фильтр"]["пер_кандидатные_вердикты"].append(
            {"актив": "USO.US", "направление": "лонг", "тайминг": "РАНО"})
    if ideas:
        p["этап6_синтез"]["отчёты"] = [{
            "актив": "BNO.US", "направление": "лонг", "балл": 0.7,
            "позиция": {"amount_usd": 500.0},
            "отчёт": {"judgment": {"поля": {
                "1_актив_направление_инструмент": "BNO.US лонг",
                "2_каскадная_цепочка": ["триггер: эскалация в Заливе", "перенос на BNO.US"],
                "11_что_неизвестно": ["глубина стакана"],
                "13_рамка": "исследовательский инструмент"}}},
        }]
        p["воронка_отсева"]["этап6_выдано_топ"] = 1
    return p


def _write_proto(logs, p):
    (pathlib.Path(logs) / f"{p['run_id']}.json").write_text(
        json.dumps(p, ensure_ascii=False), encoding="utf-8")
    return p


def _make_db(db_path, symbol, closes):
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL)")
    con.executemany("INSERT INTO quotes VALUES (?,?,?)",
                    [(symbol, d, c) for d, c in closes])
    con.commit()
    con.close()


# ── FakeTelegram ────────────────────────────────────────────────────────────────────
class FakeTelegram:
    def __init__(self):
        self.sent = []        # (chat, text, reply_markup)
        self.edits = []       # (chat, message_id, reply_markup)
        self.answers = []     # (text, show_alert)
        self._mid = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._mid

    def edit_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append((chat_id, message_id, reply_markup))
        return {}

    def answer_callback(self, cb_id, text=None, show_alert=False):
        self.answers.append((text, show_alert))
        return {}

    def get_updates(self, offset, timeout=0):
        return []


def _fresh_bot(chat="555"):
    return BOT.Bot(FakeTelegram(), chat, state=S._default_state())


def _cb(token, code="a", chat="555"):
    return {"id": "cb1", "data": f"d:{code}:{token}",
            "message": {"message_id": 10, "chat": {"id": int(chat)}}}


# ── §12: пауза пре-коммитмента ───────────────────────────────────────────────────────
def test_precommit_gate_boundaries():
    issued = "2026-06-12T00:00:00+00:00"
    before = datetime.datetime(2026, 6, 12, 23, 0, tzinfo=UTC)   # 23 ч
    after = datetime.datetime(2026, 6, 13, 0, 1, tzinfo=UTC)     # 24 ч 1 мин
    assert S.accept_unlocked(issued, before) is False
    assert S.accept_unlocked(issued, after) is True
    assert 0.9 < S.hours_remaining(issued, before) < 1.1
    assert S.hours_remaining(issued, after) == 0.0


def test_decision_and_motive_appendonly(paths):
    S.append_decision(run_id="r1", asset="BNO.US", direction="лонг", score=0.7,
                      action="reject", issued_at="2026-06-12T14:00:00+00:00", chat_id="555")
    S.append_motive(run_id="r1", asset="BNO.US", action="reject",
                    motive="контанго съедает carry", chat_id="555")
    rows = [json.loads(l) for l in paths["decisions"].read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["type"] == "decision" and rows[0]["action"] == "reject"
    assert rows[0]["motive"] is None
    assert rows[1]["type"] == "motive" and "контанго" in rows[1]["motive"]


# ── §17: лист ожидания и триггеры ────────────────────────────────────────────────────
def test_extract_early_ideas():
    early = W.extract_early_ideas(_proto())
    assert [e["актив"] for e in early] == ["USO.US"]   # только РАНО, не ВОВРЕМЯ


def test_trigger_met_and_evaluate(paths):
    _make_db(paths["db"], "BNO.US", [("2026-06-10", 70.0), ("2026-06-12", 76.0)])
    trig = W.make_trigger("BNO.US", 75.0, "above")
    W.add_entry(asset="Brent", trigger=trig, source="cli")
    # мануальная запись (без триггера) не должна авто-срабатывать
    W.add_entry(asset="Copper", trigger_text="войти когда дефицит", source="cli")
    fired = W.evaluate()
    assert len(fired) == 1
    assert fired[0]["entry"]["asset"] == "Brent"
    assert fired[0]["observed"]["close"] == 76.0
    assert W.trigger_met(W.make_trigger("X", 75, "below"), 76.0) is False


def test_watchlist_fold_fire_cancel(paths):
    e = W.add_entry(asset="Brent", trigger=W.make_trigger("BNO.US", 75, "above"))
    assert e["id"] in W.current_entries()
    W.fire_entry(e, {"date": "2026-06-12", "close": 76.0})
    assert e["id"] not in W.current_entries()   # fire закрывает armed-запись


def test_ingest_protocol_dedup(paths):
    p = _proto()
    a1 = W.ingest_protocol(p)
    assert len(a1) == 1 and a1[0]["asset"] == "USO.US"
    a2 = W.ingest_protocol(p)                    # повторно — дедуп
    assert a2 == []


# ── кнопки решений + allow-list ──────────────────────────────────────────────────────
def _register_idea(bot, token, issued_at):
    bot.state["pending"][token] = {"run_id": "r1", "asset": "BNO.US", "direction": "лонг",
                                   "score": 0.7, "issued_at": issued_at,
                                   "message_id": 10, "status": "pending"}


def test_accept_locked_does_not_record(paths):
    bot = _fresh_bot()
    token = "tok123"
    _register_idea(bot, token, S.iso(S.now_utc()))   # выдано только что → заблокировано
    bot._on_callback(_cb(token, "a"))
    assert not paths["decisions"].exists()           # решение НЕ записано
    assert bot.state["pending"][token]["status"] == "pending"
    assert bot.tg.answers and bot.tg.answers[-1][1] is True   # show_alert с подсказкой


def test_accept_unlocked_records(paths):
    bot = _fresh_bot()
    token = "tok123"
    old = S.iso(S.now_utc() - datetime.timedelta(hours=25))
    _register_idea(bot, token, old)
    bot._on_callback(_cb(token, "a"))
    rows = [json.loads(l) for l in paths["decisions"].read_text().splitlines()]
    assert rows[0]["action"] == "accept" and rows[0]["precommit_ok"] is True
    assert bot.state["pending"][token]["status"] == "accept"
    assert bot.tg.edits and bot.tg.edits[-1][2] == {"inline_keyboard": []}  # клавиатура снята


def test_reject_records_immediately(paths):
    bot = _fresh_bot()
    token = "tok123"
    _register_idea(bot, token, S.iso(S.now_utc()))   # даже свежая — reject доступен
    bot._on_callback(_cb(token, "r"))
    rows = [json.loads(l) for l in paths["decisions"].read_text().splitlines()]
    assert rows[0]["action"] == "reject"


def test_motive_capture_after_decision(paths):
    bot = _fresh_bot()
    token = "tok123"
    _register_idea(bot, token, S.iso(S.now_utc()))
    bot._on_callback(_cb(token, "r"))
    bot._on_message({"chat": {"id": 555}, "text": "продавец прав, идём мимо"})
    rows = [json.loads(l) for l in paths["decisions"].read_text().splitlines()]
    assert any(r["type"] == "motive" and "продавец" in r["motive"] for r in rows)
    assert bot.state.get("awaiting_motive") is None


def test_allow_list_blocks_foreign_chat(paths):
    bot = _fresh_bot(chat="555")
    token = "tok123"
    old = S.iso(S.now_utc() - datetime.timedelta(hours=25))
    _register_idea(bot, token, old)
    bot._on_callback(_cb(token, "a", chat="999"))    # чужой чат
    assert not paths["decisions"].exists()
    assert bot.state["pending"][token]["status"] == "pending"


# ── пуш отчётов + baseline ───────────────────────────────────────────────────────────
def test_tick_reports_push_and_dedup(paths):
    _write_proto(paths["logs"], _proto())
    bot = _fresh_bot()
    bot._tick_reports()
    pushed = [s for s in bot.tg.sent if "ИДЕЯ" in s[1]]
    assert len(pushed) == 1
    assert pushed[0][2] and "inline_keyboard" in pushed[0][2]   # клавиатура есть
    # РАНО-идея ушла в лист ожидания
    assert any(e["asset"] == "USO.US" for e in W.current_entries().values())
    # повторный тик — без дублей
    before = len(bot.tg.sent)
    bot._tick_reports()
    assert len(bot.tg.sent) == before


def test_weak_day_push(paths):
    _write_proto(paths["logs"], _proto(ideas=False, early=False))
    bot = _fresh_bot()
    bot._tick_reports()
    assert any("слабый день" in s[1] for s in bot.tg.sent)


def test_baseline_marks_without_spam(paths):
    _write_proto(paths["logs"], _proto())
    bot = _fresh_bot()
    bot.initialize_baseline()
    assert bot.state["initialized"] is True
    assert "funnel_20260612T140000Z" in bot.state["pushed_runs"]
    assert bot.tg.sent == []           # ни одного пуша из бэклога
    bot._tick_reports()
    assert bot.tg.sent == []           # уже помечено — новых пушей нет


def test_watchlist_alert_fires_once(paths):
    _make_db(paths["db"], "BNO.US", [("2026-06-12", 76.0)])
    bot = _fresh_bot()
    e = W.add_entry(asset="Brent", trigger=W.make_trigger("BNO.US", 75, "above"))
    bot._tick_watchlist()
    assert any("Сработал триггер" in s[1] for s in bot.tg.sent)
    assert e["id"] in bot.state["fired_triggers"]
    before = len(bot.tg.sent)
    bot._tick_watchlist()              # дедуп: повторно не алертит
    assert len(bot.tg.sent) == before


# ── новый «форбсовский» формат карточки ──────────────────────────────────────────────
def test_report_is_column_not_dict_dump(paths):
    proto = _proto()
    idea = R.ideas_from_protocol(proto)[0]
    text = R.format_report(proto, idea)
    assert "ИДЕЯ" in text                       # хедлайн (на него опирается пуш/поиск)
    assert "Все 13 граней" in text and "🎯 ПОЧЕМУ" in text  # читаемый лид + полнота
    # ВСЕ 13 граней присутствуют (ничего не теряем при свёртке)
    for n in range(1, 14):
        assert f"\n{n}. " in text, f"пропала грань {n}"
    assert "{" not in text and "judgment" not in text     # без дампа словаря/служебных ключей
    assert "исследовательский инструмент" in text          # дисклеймер §8 п.13


# ── свободный диалог с Дирижёром (клиент внедрён, без сети) ───────────────────────────
class _FakeLLM:
    def __init__(self, text="Коротко: бюджет в норме, идей сегодня нет."):
        self.text = text
        self.calls = []

    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        self.calls.append({"role": role, "system": system, "user": user, "agent_id": agent_id})
        return {"text": self.text, "model": "test/fake", "usage": {}, "cost": 0.0}


def test_chat_free_text_routes_to_conductor(paths, monkeypatch):
    import bot_chat as C
    fake = _FakeLLM()
    # answer() с внедрённым фейковым клиентом — без сети/ключа
    monkeypatch.setattr(C, "answer", lambda text, history=None, client=None: (
        fake.complete("conductor", C.SYSTEM_PROMPT, C.build_user_message(text, history),
                      agent_id="bot_chat", output_kind="chat")["text"], 0.0))
    bot = _fresh_bot()
    bot._on_message({"chat": {"id": 555}, "text": "что у нас с бюджетом?"})
    assert any("бюджет" in s[1].lower() for s in bot.tg.sent)        # ответ ушёл пользователю
    assert bot.state["chat_history"][-2]["role"] == "user"          # история пары записана
    assert bot.state["chat_history"][-1]["role"] == "assistant"


def test_chat_answer_uses_conductor_role_and_grounds(paths):
    import bot_chat as C
    fake = _FakeLLM("ок")
    reply, cost = C.answer("объясни воронку", history=[], client=fake)
    assert reply == "ок"
    assert fake.calls[0]["role"] == "conductor"
    assert "СОСТОЯНИЕ СИСТЕМЫ" in fake.calls[0]["user"]   # ответ заземлён на контекст
