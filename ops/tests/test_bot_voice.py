# -*- coding: utf-8 -*-
"""Тесты голосового контура бота (ops/bot_voice.py + bot._voice_text).

Закрепляют:
  • голосовое → STT → текст идёт по ОБЫЧНОМУ роутеру (интент «дай идеи» → сессия партнёра);
  • голосом можно надиктовать МОТИВ к решению (§12) — слот awaiting_motive принимает STT-текст;
  • гейты: бюджет §11 (потолок → не распознаём), нет ключа OpenRouter → честный отказ,
    слишком длинное голосовое → отказ без трат;
  • П8: «НЕРАЗБОРЧИВО» от модели → пустая строка → бот говорит «не разобрал», команды не выдумывает;
  • transcribe: разбор ответа OpenRouter, строка в costs.jsonl (agent=voice_transcriber),
    фолбек на следующую модель цепочки при сбое первой;
  • ogg_to_mp3 без ffmpeg → None (не сбой: пробуем исходный формат).
Сети нет: Telegram и requests замоканы.
"""
import sys
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import bot_state as S        # noqa: E402
import bot as BOT            # noqa: E402
import bot_voice as V        # noqa: E402
from orchestrator import openrouter as OR   # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent = []        # (chat, text, reply_markup)
        self.files = {}       # file_id -> bytes
        self._mid = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._mid

    def send_chat_action(self, chat_id, action="typing"):
        return {}

    def get_updates(self, offset, timeout=0):
        return []

    def get_file(self, file_id):
        return {"file_path": f"voice/{file_id}.oga"} if file_id in self.files else None

    def download_file(self, file_path):
        fid = file_path.split("/")[-1].split(".")[0]
        return self.files.get(fid)


def _fresh_bot(chat="555"):
    return BOT.Bot(FakeTelegram(), chat, state=S._default_state())


def _voice_msg(chat="555", file_id="f1", duration=5):
    return {"chat": {"id": int(chat)}, "voice":
            {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"}}


@pytest.fixture
def budget_ok(monkeypatch):
    monkeypatch.setattr(BOT.Bot, "_budget_status", lambda self: {"exit_code": 0, "status": "OK"})


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


# ── роутинг распознанного текста ────────────────────────────────────────────────────
def test_voice_routes_to_session(monkeypatch, budget_ok, with_key):
    """«дай идеи» голосом = «дай идеи» текстом: интент П3 срабатывает."""
    b = _fresh_bot()
    b.tg.files["f1"] = b"OGGDATA"
    monkeypatch.setattr(V, "voice_to_text", lambda tg, voice, run_id=None: "дай идеи")
    called = []
    monkeypatch.setattr(BOT.Bot, "_cmd_session", lambda self, chat: called.append(chat))
    b._on_message(_voice_msg())
    assert called, "интент «дай идеи» из голосового не дошёл до сессии партнёра"
    echoes = [t for _, t, _ in b.tg.sent if "Услышал" in t]
    assert echoes and "дай идеи" in echoes[0], "нет эха «что услышал» (П8)"


def test_voice_motive_slot(monkeypatch, tmp_path, budget_ok, with_key):
    """Мотив к решению §12 можно надиктовать голосом."""
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    b = _fresh_bot()
    b.state["awaiting_motive"] = {"token": "t1", "run_id": "r1", "asset": "GEV.US", "action": "reject"}
    monkeypatch.setattr(V, "voice_to_text", lambda tg, voice, run_id=None: "не верю в каскад")
    b._on_message(_voice_msg())
    rows = [json.loads(x) for x in (tmp_path / "decisions_user.jsonl").read_text("utf-8").splitlines()]
    assert any(r.get("motive") == "не верю в каскад" for r in rows), "мотив голосом не записан"
    assert b.state["awaiting_motive"] is None


# ── гейты ───────────────────────────────────────────────────────────────────────────
def test_voice_budget_gate(monkeypatch, with_key):
    """Потолок бюджета → голосовое не распознаём (лимит §11), трат нет."""
    b = _fresh_bot()
    monkeypatch.setattr(BOT.Bot, "_budget_status", lambda self: {"exit_code": 3, "status": "ПРЕВЫШЕНИЕ"})
    monkeypatch.setattr(V, "voice_to_text",
                        lambda *a, **k: pytest.fail("STT не должен вызываться при потолке бюджета"))
    b._on_message(_voice_msg())
    assert any("лимит" in t for _, t, _ in b.tg.sent)


def test_voice_no_key(monkeypatch, budget_ok):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    b = _fresh_bot()
    b._on_message(_voice_msg())
    assert any("OpenRouter" in t for _, t, _ in b.tg.sent)


def test_voice_too_long(monkeypatch, budget_ok, with_key):
    b = _fresh_bot()
    monkeypatch.setattr(V, "voice_to_text",
                        lambda *a, **k: pytest.fail("STT не должен вызываться для сверхдлинного"))
    b._on_message(_voice_msg(duration=V.MAX_VOICE_SEC + 1))
    assert any("длиннее" in t for _, t, _ in b.tg.sent)


def test_voice_unintelligible(monkeypatch, budget_ok, with_key):
    """'' от STT (модель сказала НЕРАЗБОРЧИВО) → «не разобрал», команд не выдумываем (П8)."""
    b = _fresh_bot()
    monkeypatch.setattr(V, "voice_to_text", lambda tg, voice, run_id=None: "")
    b._on_message(_voice_msg())
    assert any("Не разобрал" in t for _, t, _ in b.tg.sent)
    assert not any("Услышал" in t for _, t, _ in b.tg.sent)


def test_voice_stt_error_is_soft(monkeypatch, budget_ok, with_key):
    """Сбой STT не роняет бота — честное сообщение владельцу."""
    def _boom(tg, voice, run_id=None):
        raise RuntimeError("все модели роли voice_transcriber исчерпаны")
    b = _fresh_bot()
    monkeypatch.setattr(V, "voice_to_text", _boom)
    b._on_message(_voice_msg())
    assert any("Не смог распознать" in t for _, t, _ in b.tg.sent)


# ── transcribe: разбор ответа, costs.jsonl, фолбек цепочки ──────────────────────────
class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


def test_transcribe_parses_and_logs_cost(monkeypatch, tmp_path, with_key):
    monkeypatch.setattr(OR, "COSTS_LOG", tmp_path / "costs.jsonl")
    payload = {"choices": [{"message": {"content": " «разбери GEV» "}}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.0001}}
    sent_bodies = []

    def _post(url, headers=None, json=None, timeout=None):
        sent_bodies.append(json)
        return _Resp(payload)

    monkeypatch.setattr(V.requests, "post", _post)
    out = V.transcribe(b"MP3DATA", "mp3")
    assert out == "разбери GEV"
    body = sent_bodies[0]
    parts = body["messages"][1]["content"]
    assert any(p.get("type") == "input_audio" and p["input_audio"]["format"] == "mp3" for p in parts)
    rows = [json.loads(x) for x in (tmp_path / "costs.jsonl").read_text("utf-8").splitlines()]
    assert rows and rows[0]["agent"] == "voice_transcriber" and rows[0]["ok"] is True


def test_transcribe_fallback_chain(monkeypatch, tmp_path, with_key):
    """Первая модель 500 → фолбек на вторую (§26); обе попытки в costs.jsonl."""
    monkeypatch.setattr(OR, "COSTS_LOG", tmp_path / "costs.jsonl")
    calls = []

    def _post(url, headers=None, json=None, timeout=None):
        calls.append(json["model"])
        if len(calls) == 1:
            return _Resp({}, status=500)
        return _Resp({"choices": [{"message": {"content": "дай идеи"}}], "usage": {}})

    monkeypatch.setattr(V.requests, "post", _post)
    assert V.transcribe(b"A", "mp3") == "дай идеи"
    assert len(calls) == 2 and calls[0] != calls[1]
    rows = [json.loads(x) for x in (tmp_path / "costs.jsonl").read_text("utf-8").splitlines()]
    assert [r["ok"] for r in rows] == [False, True]


def test_transcribe_unintelligible_empty(monkeypatch, tmp_path, with_key):
    monkeypatch.setattr(OR, "COSTS_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setattr(V.requests, "post", lambda *a, **k: _Resp(
        {"choices": [{"message": {"content": "НЕРАЗБОРЧИВО"}}], "usage": {}}))
    assert V.transcribe(b"A", "mp3") == ""


def test_transcribe_all_models_fail(monkeypatch, tmp_path, with_key):
    monkeypatch.setattr(OR, "COSTS_LOG", tmp_path / "costs.jsonl")
    monkeypatch.setattr(V.requests, "post", lambda *a, **k: _Resp({}, status=503))
    with pytest.raises(RuntimeError, match="исчерпаны"):
        V.transcribe(b"A", "mp3")


# ── ffmpeg-путь ─────────────────────────────────────────────────────────────────────
def test_ogg_to_mp3_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(V.shutil, "which", lambda name: None)
    assert V.ogg_to_mp3(b"OGG") is None


def test_voice_to_text_raw_fallback_hints_ffmpeg(monkeypatch, with_key):
    """Без ffmpeg и с отказом моделей на ogg — в ошибке подсказка поставить ffmpeg."""
    tg = FakeTelegram()
    tg.files["f1"] = b"OGGDATA"
    monkeypatch.setattr(V.shutil, "which", lambda name: None)

    def _fail(audio, fmt, run_id=None):
        raise RuntimeError("все модели роли voice_transcriber исчерпаны: ogg не принят")

    monkeypatch.setattr(V, "transcribe", _fail)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        V.voice_to_text(tg, {"file_id": "f1", "mime_type": "audio/ogg"})
