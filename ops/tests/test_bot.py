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


def test_case_feedback_captured_on_variant_match(paths):
    """Этап3: ответ на вопрос «Разбора дня», совпавший с вариантом, пишется как case_feedback (§25)."""
    bot = _fresh_bot()
    bot.state["last_case"] = {"run_id": "ef_T", "asset": "GEV.US", "status": "live_candidate",
                              "варианты": ["убедительно, копнуть", "связь притянута", "мимо интереса"]}
    bot._on_message({"chat": {"id": 555}, "text": "связь притянута"})
    rows = [json.loads(l) for l in paths["decisions"].read_text().splitlines()]
    assert any(r["type"] == "case_feedback" and r["answer"] == "связь притянута"
               and r["case_status"] == "live_candidate" for r in rows)
    assert bot.state.get("last_case") is None       # разметка снята, повторно не ловим


def test_case_feedback_ignores_unrelated_question(paths):
    """Обычный вопрос НЕ перехватывается как разметка кейса (роутится дальше к Дирижёру)."""
    bot = _fresh_bot()
    bot.state["last_case"] = {"run_id": "ef_T", "asset": "GEV.US", "status": "live_candidate",
                              "варианты": ["убедительно, копнуть", "связь притянута", "мимо интереса"]}
    captured = bot._capture_case_feedback(555, "а что там с инфляцией в Индии на самом деле?")
    assert captured is False
    assert not paths["decisions"].exists()          # ничего не записано
    assert bot.state.get("last_case") is not None    # кейс ещё ждёт ответа


def test_case_feedback_does_not_swallow_short_common_words(paths):
    """Регрессия stage-review: короткое «да»/«нет»/«не знаю» НЕ перехватывается как разметка кейса
    (раньше подстрочный матч головы варианта глотал их и писал ложную запись §25)."""
    bot = _fresh_bot()
    bot.state["last_case"] = {"run_id": "ef_T", "asset": "ADM.US", "status": "candidate_autopsy",
                              "варианты": ["да, идея слабая", "нет, суд ошибся", "нужен глубокий разбор"]}
    for msg in ("да", "нет", "не знаю", "ок"):
        assert bot._capture_case_feedback(555, msg) is False, msg
    assert not paths["decisions"].exists()          # ни одной ложной записи
    assert bot.state.get("last_case") is not None
    # полный вариант — ловится
    assert bot._capture_case_feedback(555, "да, идея слабая") is True


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
    pushed = [s for s in bot.tg.sent if "Идея дня:" in s[1]]
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
    assert any("Идей под ставку сегодня нет" in s[1] for s in bot.tg.sent)


def test_mock_run_not_pushed_by_default(paths):
    # mock-прогон не маскируется под идею и по умолчанию вовсе не шлётся (send_mock_to_telegram=false)
    p = _proto()
    p["mode"] = "mock"
    _write_proto(paths["logs"], p)
    bot = _fresh_bot()
    bot._tick_reports()
    assert bot.tg.sent == []                                   # ничего не отправлено
    assert p["run_id"] in bot.state["pushed_runs"]             # но помечен как обработанный


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
    assert any("Сработал ориентир" in s[1] for s in bot.tg.sent)
    assert e["id"] in bot.state["fired_triggers"]
    before = len(bot.tg.sent)
    bot._tick_watchlist()              # дедуп: повторно не алертит
    assert len(bot.tg.sent) == before


# ── канонический формат подачи §8 (все 13 полей + ℹ️, шапка с 1 цифрой) ───────────────
def _live_idea():
    return {"актив": "BNO.US", "направление": "лонг", "балл": 0.58,
            "позиция": {"вероятность": 0.65},
            "отчёт": {"judgment": {"поля": {
                "1_": "BNO.US лонг", "2_каскад": ["триггер X", "перенос на BNO.US"],
                "9_скоринг": "критик/судья: ок", "13_рамка": "инструмент"}}}}


def test_long_report_all_13_fields_with_help(paths):
    proto = {"run_id": "f1", "mode": "live", "theme": "brent"}
    text = R.format_report(proto, _live_idea(), {"mode": "long", "still_unclear": set(),
                                                 "send_mock_to_telegram": False})
    assert "Идея дня:" in text and "нефть Brent" in text          # человеческий хедлайн
    assert "Насколько уверены: ~65%" in text                       # ОДНА цифра в шапке (поле 3)
    for n in range(1, 14):
        assert f"\n{n}. " in text, f"пропало поле {n}"            # ВСЕ 13 полей
    assert text.count("ℹ️") == 13                                  # long → ℹ️ у всех
    # сводный балл 0.58 — ТОЛЬКО в п.9 с пометкой, НЕ в шапке
    assert "0.58/1.00" in text and "сводная оценка по 6 критериям" in text
    assert "0.58" not in text.split("\n")[1]                       # не в строке уверенности
    assert "{" not in text and "judgment" not in text


def test_short_mode_shows_help_only_for_marked():
    proto = {"run_id": "f1", "mode": "live", "theme": "brent"}
    text = R.format_report(proto, _live_idea(), {"mode": "short", "still_unclear": {2},
                                                 "send_mock_to_telegram": False})
    assert text.count("ℹ️") == 1                                   # только поле 2
    assert "→" in text                                             # содержание всегда есть


def test_empty_field_is_honest_not_invented():
    proto = {"run_id": "f1", "mode": "live", "theme": "brent"}
    text = R.format_report(proto, _live_idea(), {"mode": "long", "still_unclear": set(),
                                                 "send_mock_to_telegram": False})
    assert R.EMPTY_MARK in text                                    # пустые поля помечены честно (П8)


def test_mock_is_connection_check_not_idea():
    proto = {"run_id": "f1", "mode": "mock", "theme": "brent"}
    text = R.format_report(proto, _live_idea())
    assert "Проверка связи" in text and "НЕ идея" in text          # не маскируется под идею
    assert "Идея дня" not in text


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


def test_chat_answer_uses_partner_role_and_grounds(paths):
    # решение владельца 05.07: мозг чата — partner_chat (Fable 5), персона спорящего партнёра
    import bot_chat as C
    fake = _FakeLLM("ок")
    reply, cost = C.answer("объясни воронку", history=[], client=fake)
    assert reply == "ок"
    assert fake.calls[0]["role"] == "partner_chat"
    assert "СОСТОЯНИЕ СИСТЕМЫ" in fake.calls[0]["user"]   # ответ заземлён на контекст
    assert "П8" in C.SYSTEM_PROMPT and "П10" in C.SYSTEM_PROMPT   # границы честности в персоне
    assert "спор" in C.SYSTEM_PROMPT.lower()                      # партнёр спорит, а не диспетчерит


# ── состязательный разбор идеи по возражению (/debate, §4 блок E) ─────────────────────
@pytest.fixture
def challenge_paths(paths, monkeypatch):
    """Изоляция движка разбора: читаем идеи из temp funnel_logs, пишем разбор в temp."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from orchestrator import challenge as CH
    from orchestrator import openrouter as OR
    monkeypatch.setattr(CH, "FUNNEL_LOGS", paths["logs"])
    monkeypatch.setattr(CH, "CHALLENGE_LOGS", paths["tmp"] / "challenges")
    # Герметичность: разбор гоняем на MockClient, не на сети (mode="auto" иначе ушёл бы в live
    # при ключе в окружении — реальные вызовы и трата бюджета в юнит-тесте).
    monkeypatch.setattr(OR, "make_client",
                        lambda mode="auto", models=None, run_id=None: OR.MockClient(models, run_id))
    (paths["logs"] / "funnel_20260612T140000Z.json").write_text(
        json.dumps(_proto(), ensure_ascii=False), encoding="utf-8")
    return CH


# ── запуск тяжёлых задач из чата (прогон/калибровка/сверка) ───────────────────────────
def test_intent_run_funnel_matches_natural_phrasing():
    yes = ["сделай прогон", "Да, сделай прогон", "запусти прогон", "прогон", "найди идеи",
           "поищи идеи", "сделай прогон воронки", "новый прогон"]
    no = ["что у нас с бюджетом?", "объясни последнюю идею про прогон воронки и почему медь",
          "почему ты не нашёл идей вчера", "расскажи как устроен прогон", ""]
    for t in yes:
        assert BOT.Bot._intent_run_funnel(t) is True, t
    for t in no:
        assert BOT.Bot._intent_run_funnel(t) is False, t


def test_run_funnel_refuses_without_key(paths, monkeypatch):
    bot = _fresh_bot()
    monkeypatch.setattr(bot, "_budget_status", lambda: {"exit_code": 0})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    bot._cmd_run_funnel(555)
    assert any("ключ" in s[1].lower() for s in bot.tg.sent)
    assert not bot._job_busy()                              # задача не запущена


def test_run_funnel_blocked_by_budget(paths, monkeypatch):
    bot = _fresh_bot()
    monkeypatch.setattr(bot, "_budget_status", lambda: {"exit_code": 3})
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    bot._cmd_run_funnel(555)
    assert any("лимит расходов" in s[1] for s in bot.tg.sent)
    assert not bot._job_busy()


def test_run_funnel_launches_background_job(paths, monkeypatch):
    import orchestrator.event_first as EF
    called = {}

    def _stub(mode="mock", k=3, skip_contour=False, write=True, **kw):
        called.update(mode=mode, k=k, skip_contour=skip_contour, write=write)
        return {"run_id": "ef_test", "итог": "источников 0; идей 0"}

    monkeypatch.setattr(EF, "run_event_first", _stub)
    bot = _fresh_bot()
    monkeypatch.setattr(bot, "_budget_status", lambda: {"exit_code": 0})
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    bot._cmd_run_funnel(555)
    assert any("Запускаю боевой прогон" in s[1] for s in bot.tg.sent)   # моментальный ack
    bot._job.join(timeout=5)
    assert called == {"mode": "live", "k": 2, "skip_contour": True, "write": True}


def test_only_one_heavy_job_at_a_time(paths, monkeypatch):
    import orchestrator.event_first as EF
    release = __import__("threading").Event()
    monkeypatch.setattr(EF, "run_event_first",
                        lambda **kw: release.wait(timeout=5) and {"run_id": "x", "итог": ""})
    bot = _fresh_bot()
    monkeypatch.setattr(bot, "_budget_status", lambda: {"exit_code": 0})
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    bot._cmd_run_funnel(555)                                # первая — стартует
    assert bot._job_busy()
    n = len(bot.tg.sent)
    bot._cmd_calibrate(555)                                 # вторая (любая тяжёлая) — отказ
    assert any("Уже идёт задача" in s[1] for s in bot.tg.sent[n:])
    release.set()
    bot._job.join(timeout=5)


def test_resolve_runs_and_reports(paths, monkeypatch):
    import orchestrator.resolve as RES
    monkeypatch.setattr(RES, "run_resolve", lambda write=True: {
        "сверено_сейчас": 2, "ещё_pending": 5, "всего_исходов": 20, "brier": 0.21,
        "калибровка_band_пп": 7, "до_ворот_270": 250, "ошибок": 0})
    bot = _fresh_bot()
    bot._cmd_resolve(555)
    bot._job.join(timeout=5)
    joined = "\n".join(s[1] for s in bot.tg.sent)
    assert "Сверяю созревшие" in joined                     # ack
    assert "Сверка готова" in joined and "до ворот 270 осталось 250" in joined


def test_help_lists_new_run_commands():
    bot = _fresh_bot()
    bot._command(555, "/help")
    txt = " ".join(s[1] for s in bot.tg.sent)
    assert "/run-funnel" in txt and "/calibrate" in txt and "/resolve" in txt
    assert "веса и лимиты" in txt.lower()                    # инвариант: не из чата


def test_debate_no_args_lists_ideas(challenge_paths):
    bot = _fresh_bot()
    bot._on_message({"chat": {"id": 555}, "text": "/debate"})
    txt = " ".join(s[1] for s in bot.tg.sent)
    assert "оспорить" in txt.lower() and "BNO.US" in txt        # перечислил идею, не запускал контур


def test_debate_runs_contour_and_returns_verdict(challenge_paths):
    bot = _fresh_bot()
    bot._on_message({"chat": {"id": 555},
                     "text": "/debate это же просто ставка на нефть, всё уже в цене"})
    # Разбор идёт в ФОНОВОМ потоке (не морозит опрос Telegram) — дождёмся доставки вердикта.
    if bot._job is not None:
        bot._job.join(timeout=30)
    joined = "\n".join(s[1] for s in bot.tg.sent)
    assert "состязательн" in joined.lower()                     # анонс запуска
    assert "Вердикт слепого судьи" in joined                    # резюме контура доставлено
    assert "не рекомендация" in joined                          # рамка §12
    # разбор записан в изолированный журнал, не в боевой
    assert list((challenge_paths.CHALLENGE_LOGS).glob("challenge_*.json"))


# ── заземление свободного чата на ВЫДАННЫЕ идеи (баг: Дирижёр не видел пушнутую карточку) ──
def test_chat_brief_grounds_on_pending_ideas_not_mock_fixture(paths, monkeypatch):
    import bot_chat as BC
    # боевой прогон с идеей + mock-фикстура, которая по имени файла «последняя» (week7…)
    real = _proto(run_id="multi_20260613T222357Z__ai_power", issued_at="2026-06-13T22:23:57Z")
    real["mode"] = "live"; real["theme"] = "ai_power"
    real["этап6_синтез"]["отчёты"][0]["актив"] = "CLF.US"
    real["этап6_синтез"]["отчёты"][0]["позиция"] = {"вероятность": 0.65}
    real["этап6_синтез"]["отчёты"][0]["отчёт"]["judgment"]["поля"] = {
        "1_актив_направление_инструмент": "CLF.US лонг (сталь GOES)",
        "2_каскадная_цепочка": ["спрос на трансформаторы", "дефицит GOES-стали", "перенос на CLF.US"],
        "11_что_неизвестно": ["доля выручки CLF от GOES-стали", "лаг переноса спроса в цену"]}
    (paths["logs"] / "multi_20260613T222357Z__ai_power.json").write_text(
        json.dumps(real, ensure_ascii=False), encoding="utf-8")
    fixture = _proto(run_id="week7_testday", issued_at="")
    fixture["mode"] = "mock"
    (paths["logs"] / "week7_testday.json").write_text(
        json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    # состояние бота: CLF.US выдан и ждёт решения
    st = S._default_state()
    st["pending"]["multi_20260613T222357Z__ai_power|CLF.US"] = {
        "run_id": "multi_20260613T222357Z__ai_power", "asset": "CLF.US", "direction": "лонг",
        "issued_at": "2026-06-13T22:23:57Z", "status": "pending"}
    monkeypatch.setattr(S, "load_state", lambda path=None: st)

    brief = BC.context_brief()
    assert "CLF.US" in brief                                  # выданная идея видна Дирижёру
    assert "GOES" in brief                                    # суть восстановлена из протокола
    assert "week7_testday" not in brief                       # mock-фикстура не выдаётся за прогон
    # «последний боевой прогон» — именно live ai_power, не mock-фикстура
    assert "ai_power" in brief
