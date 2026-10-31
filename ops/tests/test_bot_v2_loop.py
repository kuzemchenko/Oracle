# -*- coding: utf-8 -*-
"""Тесты V2 (карта v3): петля владельца — кнопки «Идеи дня», очередь прокачки,
еженедельная карточка тем. Герметично: Telegram и пути — фейки/tmp, сети нет.

Закрепляют:
  • пуш «Идеи дня» уходит С КНОПКАМИ [Качаем 2 дня][Что ещё было][Мимо] (аудит 31.10:
    ноль нажатий за жизнь системы, потому что нажимать было не на что);
  • «Качаем» → запись deep_dive в decisions_user.jsonl (§12) + очередь journal/deep_dive.jsonl
    + немедленный запуск состязательного разбора; повторное нажатие — дедуп;
  • «Мимо» → запись skip; «Что ещё было» → показ Топ-3 того же прогона;
  • устаревший callback (чужой run_id) не пишет ничего;
  • bot_themes: дедуп очереди по ключу, карточка ≤5 тем, решения в theme_decisions.jsonl,
    недельный дедуп карточки, кнопки y/n снимают тему из pending.
"""
import json
import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import bot_state as S        # noqa: E402
import bot as BOT            # noqa: E402
import bot_themes as TH      # noqa: E402
import bot_reports as R      # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent = []        # (chat, text, reply_markup)
        self.answers = []     # (cb_id, text)
        self.edits = []
        self._mid = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._mid

    def edit_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append((chat_id, message_id, reply_markup))
        return {}

    def answer_callback(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text))
        return {}

    def get_updates(self, offset, timeout=0):
        return []


def _bot(chat="555"):
    return BOT.Bot(FakeTelegram(), chat, state=S._default_state())


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)          # deep_dive.jsonl / notices
    return tmp_path


def _cb(data, chat=555, mid=77):
    return {"id": "cb1", "data": data,
            "message": {"message_id": mid, "chat": {"id": chat}}}


# ── кнопки на пуше «Идеи дня» ────────────────────────────────────────────────────────
def test_idea_day_push_carries_buttons(paths, monkeypatch):
    proto = {"run_id": "ef_x", "ts": "2026-11-02T09:00:00+00:00", "mode": "live"}
    b = _bot()
    monkeypatch.setattr(BOT.R, "load_presentation",
                        lambda: {"send_mock_to_telegram": False, "idea_day_lead": True,
                                 "top3_lead": False, "daily_case_lead": False,
                                 "mode": "short", "still_unclear": set()})
    monkeypatch.setattr(BOT.R, "new_runs", lambda pushed: [proto])
    monkeypatch.setattr(BOT.R, "idea_of_day_from_protocol",
                        lambda p, **kw: ("Идея дня — текст.", {"idx": {"1": "LNG.US"},
                                                               "lead": "LNG.US"}, []))
    monkeypatch.setattr(BOT.R, "money_ideas_from_protocol", lambda p: [])
    monkeypatch.setattr(BOT.W, "ingest_protocol", lambda p, existing_ids=None: [])
    b._tick_reports()
    assert b.tg.sent, "пуш «Идеи дня» не ушёл"
    _, text, kb = b.tg.sent[-1]
    assert "Идея дня" in text
    labels = [btn["text"] for row in kb["inline_keyboard"] for btn in row]
    assert any("Качаем" in x for x in labels) and any("Мимо" in x for x in labels)
    datas = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
    assert f"v:q:{proto['run_id']}" in datas
    lid = b.state["last_idea_day"]
    assert lid["lead"] == "LNG.US" and lid["run_id"] == "ef_x" and lid["issued_at"]


def test_deep_dive_button_full_loop(paths, monkeypatch):
    b = _bot()
    b.state["last_idea_day"] = {"run_id": "ef_x", "lead": "LNG.US",
                                "issued_at": "2026-11-02T09:00:00+00:00", "idx": {}}
    launched = []
    monkeypatch.setattr(BOT.Bot, "_cmd_check_idea", lambda self, chat, t: launched.append(t))
    b._on_callback(_cb("v:q:ef_x"))
    # решение §12 записано
    rows = [json.loads(x) for x in (paths / "decisions_user.jsonl").read_text("utf-8").splitlines()]
    assert rows and rows[0]["action"] == "deep_dive" and rows[0]["asset"] == "LNG.US"
    # очередь прокачки V3 пополнена
    dd = [json.loads(x) for x in (paths / "journal" / "deep_dive.jsonl").read_text("utf-8").splitlines()]
    assert dd and dd[0]["asset"] == "LNG.US" and dd[0]["status"] == "queued"
    # разбор запущен, клавиатура снята, подтверждение ушло
    assert launched == ["LNG.US"]
    assert b.tg.edits and b.tg.edits[0][2] == {"inline_keyboard": []}
    assert any("прокачку" in t for _, t, _ in b.tg.sent)
    # повторное нажатие — дедуп, второй записи нет
    b._on_callback(_cb("v:m:ef_x"))
    rows = [json.loads(x) for x in (paths / "decisions_user.jsonl").read_text("utf-8").splitlines()]
    assert len(rows) == 1
    assert any("Уже" in (a or "") for _, a in b.tg.answers)


def test_skip_button_writes_skip(paths, monkeypatch):
    b = _bot()
    b.state["last_idea_day"] = {"run_id": "ef_x", "lead": "GEV.US",
                                "issued_at": "2026-11-02T09:00:00+00:00", "idx": {}}
    b._on_callback(_cb("v:m:ef_x"))
    rows = [json.loads(x) for x in (paths / "decisions_user.jsonl").read_text("utf-8").splitlines()]
    assert rows[0]["action"] == "skip" and rows[0]["asset"] == "GEV.US"
    assert not (paths / "journal" / "deep_dive.jsonl").exists()


def test_more_ideas_button_routes(paths, monkeypatch):
    b = _bot()
    b.state["last_idea_day"] = {"run_id": "ef_x", "lead": "GEV.US",
                                "issued_at": "2026-11-02T09:00:00+00:00", "idx": {}}
    shown = []
    monkeypatch.setattr(BOT.Bot, "_cmd_more_ideas", lambda self, chat: shown.append(chat))
    b._on_callback(_cb("v:w:ef_x"))
    assert shown, "кнопка «Что ещё было» не показала Топ-3"
    assert not (paths / "decisions_user.jsonl").exists(), "показ списка — не решение, в §12 не пишем"


def test_stale_callback_ignored(paths):
    b = _bot()
    b.state["last_idea_day"] = {"run_id": "ef_new", "lead": "GEV.US",
                                "issued_at": "2026-11-02T09:00:00+00:00", "idx": {}}
    b._on_callback(_cb("v:q:ef_old"))
    assert not (paths / "decisions_user.jsonl").exists()
    assert any("устарела" in (a or "") for _, a in b.tg.answers)


# ── очередь тем (bot_themes) ─────────────────────────────────────────────────────────
def _theme_row(i, pot=0.5, ts="2026-10-30T09:00:00+00:00", instruments=None):
    return {"ts": ts, "событие": f"Событие {i}", "статус": "предложено",
            "тектонический_потенциал": pot, "кластер_ключи": [f"k{i}"],
            "целевой_дальний_узел": {"node": f"Узел-следствие номер {i}",
                                     "instruments": instruments or [f"T{i}.US"]}}


def test_theme_queue_dedup_rank_and_card(tmp_path):
    proposed = tmp_path / "proposed.jsonl"
    rows = [_theme_row(i, pot=0.1 * i) for i in range(7)]
    rows.append(_theme_row(3, pot=0.3, ts="2026-10-31T09:00:00+00:00"))  # дубль темы 3 посвежее
    proposed.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    items = TH.pending_queue(proposed, tmp_path / "dec.jsonl", limit=5)
    assert len(items) == 5
    pots = [r.get("тектонический_потенциал") for _k, r in items]
    assert pots == sorted(pots, reverse=True), "сильные темы должны идти первыми"
    keys = [k for k, _ in items]
    assert len(set(keys)) == 5, "дедуп по ключу не сработал"
    card = TH.format_card(items)
    assert card.count("Бумаги:") == 5 and "✅" in card
    kb = TH.build_keyboard(items)
    assert len(kb["inline_keyboard"]) == 5
    assert kb["inline_keyboard"][0][0]["callback_data"].startswith("t:y:")


def test_theme_decision_recorded_and_excluded(tmp_path):
    proposed, dec = tmp_path / "p.jsonl", tmp_path / "d.jsonl"
    proposed.write_text(json.dumps(_theme_row(1), ensure_ascii=False), encoding="utf-8")
    (k, row), = TH.pending_queue(proposed, dec)
    TH.record_decision(k, "approved", row, "555", decisions_path=dec)
    rec = json.loads(dec.read_text("utf-8").splitlines()[0])
    assert rec["решение"] == "approved" and rec["ключ"] == k and rec["инструменты"] == ["T1.US"]
    assert TH.pending_queue(proposed, dec) == [], "разобранная тема не должна предлагаться снова"


def test_tick_theme_batch_weekly(paths, monkeypatch, tmp_path):
    b = _bot()
    monkeypatch.setattr(TH, "PROPOSED", tmp_path / "p.jsonl")
    monkeypatch.setattr(TH, "DECISIONS", tmp_path / "d.jsonl")
    (tmp_path / "p.jsonl").write_text(
        "\n".join(json.dumps(_theme_row(i), ensure_ascii=False) for i in range(3)), encoding="utf-8")
    sat = datetime.datetime(2026, 11, 7, 11, 0, tzinfo=datetime.timezone.utc)
    assert sat.isoweekday() == 6
    b._tick_theme_batch(sat)
    assert b.tg.sent and b.tg.sent[-1][2]["inline_keyboard"], "карточка тем без кнопок"
    assert len(b.state["themes_pending"]) == 3
    n = len(b.tg.sent)
    b._tick_theme_batch(sat + datetime.timedelta(hours=1))   # та же неделя — дедуп
    assert len(b.tg.sent) == n
    # решение по теме кнопкой: запись + снятие из pending
    key = next(iter(b.state["themes_pending"]))
    b._on_callback(_cb(f"t:y:{key}"))
    rec = json.loads((tmp_path / "d.jsonl").read_text("utf-8").splitlines()[0])
    assert rec["решение"] == "approved"
    assert key not in b.state["themes_pending"]
    # будний день — карточка не шлётся
    b2 = _bot()
    b2._tick_theme_batch(datetime.datetime(2026, 11, 9, 11, 0, tzinfo=datetime.timezone.utc))
    assert not b2.tg.sent
