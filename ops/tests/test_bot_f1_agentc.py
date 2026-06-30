# -*- coding: utf-8 -*-
"""Тесты агента C этапа F1 (правки ТОЛЬКО в ops/bot.py).

Закрепляют:
  • #15 — намерение «разбери ТИКЕР» / «копни ТИКЕР» / «разбери компанию X» в свободной речи
    роутится в состязательный разбор (_intent_check_idea возвращает текст с тикером), а голое
    «разбери …» без тикера — НЕ перехватывается (уходит Дирижёру);
  • #11 — для сводного ef_-прогона money-идея, ПЕРЕЖИВШАЯ слепой суд («УСТОЯЛА»), после дайджеста
    уходит ОТДЕЛЬНОЙ §8-карточкой с кнопками §12 и регистрируется в state['pending'];
    fail-closed: РАЗБИТАЯ судом money-идея карточку НЕ порождает; повтор тика — без дублей.
Telegram замокан (FakeTelegram), сети/БД нет.
"""
import sys
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))

import bot_state as S        # noqa: E402
import bot_reports as R      # noqa: E402
import bot_watchlist as W    # noqa: E402
import bot as BOT            # noqa: E402
import bot_introspect as INTRO   # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self._mid = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._mid

    def get_updates(self, offset, timeout=0):
        return []


@pytest.fixture
def paths(tmp_path, monkeypatch):
    logs = tmp_path / "funnel_logs"
    logs.mkdir()
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(W, "WATCHLIST_PATH", tmp_path / "watchlist.jsonl")
    monkeypatch.setattr(W, "DB_PATH", tmp_path / "oracle.db")
    monkeypatch.setattr(R, "FUNNEL_LOGS", logs)
    return {"logs": logs}


def _fresh_bot(chat="555"):
    return BOT.Bot(FakeTelegram(), chat, state=S._default_state())


# ── #15: распознавание «разбери ТИКЕР» / «копни ТИКЕР» / «разбери компанию X» ─────────
def test_intent_check_idea_matches_bare_ticker():
    out = BOT.Bot._intent_check_idea("разбери CLF")
    assert out and INTRO._ticker(out) == "CLF.US"


def test_intent_check_idea_matches_kopni_and_company():
    assert INTRO._ticker(BOT.Bot._intent_check_idea("копни NUE")) == "NUE.US"
    assert INTRO._ticker(BOT.Bot._intent_check_idea("разбери компанию CLF — сталь")) == "CLF.US"


def test_intent_check_idea_legacy_idea_still_matches():
    # старый матч «идею <X>» не сломан
    assert BOT.Bot._intent_check_idea("проверь идею NUE — сталь вырастет") is not None


def test_intent_check_idea_no_ticker_goes_to_chat():
    # голое «разбери …» без тикер-образного токена — это вопрос Дирижёру, не разбор идеи
    assert BOT.Bot._intent_check_idea("разбери ситуацию на рынке") is None
    assert BOT.Bot._intent_check_idea("что сейчас происходит?") is None


# ── #11: money-идея, пережившая слепой суд, → отдельная §8-карточка с кнопками ────────
def _money_node(asset="CLF.US"):
    return {"актив": asset, "направление": "лонг", "score": 0.72, "вероятность": 0.61,
            "провизорный": False, "событие": "новые пошлины на импортную сталь",
            "якорь": "steel", "порядок": 2}


def _ef_proto(outcome="УСТОЯЛА", run_id="ef_20260629T090001Z",
              issued_at="2026-06-29T09:00:00+00:00"):
    node = _money_node()
    return {"run_id": run_id, "ts": issued_at, "mode": "auto",
            "граф_отбор": {"топ_k": [node], "money_трек": [node],
                           "суд_money": {"CLF.US": {"исход": outcome}},
                           "треки": {"money": 1, "провизорный": 0}},
            "картограф_идеи": []}


def _write(logs, p):
    (pathlib.Path(logs) / f"{p['run_id']}.json").write_text(
        json.dumps(p, ensure_ascii=False), encoding="utf-8")
    return p


def test_ef_surviving_money_idea_pushes_pending_card(paths):
    _write(paths["logs"], _ef_proto(outcome="УСТОЯЛА"))
    bot = _fresh_bot()
    bot._tick_reports()
    # дайджест ушёл
    assert any("Идеи дня" in s[1] for s in bot.tg.sent)
    # отдельная §8-карточка с кнопками §12
    cards = [s for s in bot.tg.sent if "Идея дня:" in s[1]]
    assert len(cards) == 1
    assert cards[0][2] and "inline_keyboard" in cards[0][2]
    # метка «исследование, не рекомендация» сохранена (C2/FГ)
    assert "не инвестиционная рекомендация" in cards[0][1]
    # зарегистрирована в pending
    pend = bot.state["pending"]
    assert len(pend) == 1
    tok = next(iter(pend))
    assert pend[tok]["asset"] == "CLF.US"
    assert pend[tok]["idea_brief"]["актив"] == "CLF.US"
    # повтор тика — без новых карточек и без дублей в pending
    before = len(bot.tg.sent)
    bot._tick_reports()
    assert len(bot.tg.sent) == before
    assert len(bot.state["pending"]) == 1


def test_ef_demoted_money_idea_no_card(paths):
    # fail-closed §11: РАЗБИТАЯ судом money-идея НЕ доходит до §8-карточки
    _write(paths["logs"], _ef_proto(outcome="РАЗБИТА"))
    bot = _fresh_bot()
    bot._tick_reports()
    assert any("Идеи дня" in s[1] for s in bot.tg.sent)        # дайджест есть
    assert not any("Идея дня:" in s[1] for s in bot.tg.sent)   # карточки-ставки нет
    assert bot.state["pending"] == {}


# ── БЛОКЕР stage-review F1: fail-open на money-пути под --deep ─────────────────────────
def test_ef_deep_money_card_keeps_disclaimer_fail_closed():
    """Боевой крон идёт с --deep: money-карточка подменяет отчёт на LLM-judgment, и поле 13
    (рамка-дисклеймер) пришло бы из LLM. Если LLM вернул поле 13 ПУСТЫМ — метка «не инвестиционная
    рекомендация» НЕ должна исчезнуть (fix #11 «метку НЕ снимаем», §8/§11 fail-closed).
    Раньше карточка показала бы «[поле пустое]» в слоте дисклеймера и ушла без рамки."""
    node = _money_node()
    deep = {"поля": {"1. Актив/направление/инструмент": "сталь CLF",
                     "13. Рамка-дисклеймер": ""}}          # LLM вернул рамку ПУСТОЙ
    proto = {"run_id": "ef_20260629T090001Z", "ts": "2026-06-29T09:00:00+00:00", "mode": "live",
             "граф_отбор": {"топ_k": [node], "money_трек": [node],
                            "суд_money": {"CLF.US": {"исход": "УСТОЯЛА", "отчёт_§8": deep}},
                            "треки": {"money": 1, "провизорный": 0}},
             "картограф_идеи": []}
    cards = R.money_ideas_from_protocol(proto)
    assert len(cards) == 1
    assert cards[0]["отчёт"] == {"judgment": deep}            # подмена на judgment произошла (deep-путь)
    text = R.format_report(proto, cards[0])
    # рамка присутствует, несмотря на пустое поле 13: два слоя — поле13 детерминир. + безусловный футер.
    # ровно 2 вхождения доказывают, что поле13 отрисовало РАМКУ, а не «[поле пустое]» (footer даёт 1).
    assert R.RESEARCH_FRAME in text
    assert text.count("не инвестиционная рекомендация") >= 2


def test_ef_deep_money_card_disclaimer_survives_truncation():
    """Блокер re-review F1: рамка §16 стояла в хвосте, а format_report резал text[:MAX_MSG]=3800.
    На длинной --deep money-карточке (13 заполненных полей long-режима > MAX_MSG) обрезка срезала
    И поле13, И футер → actionable-карточка с кнопками уходила пользователю БЕЗ метки. Здесь карточка
    ЗАВЕДОМО длиннее лимита: рамка обязана выжить (неприкосновенный хвост доливается ПОСЛЕ усечения)."""
    node = _money_node()
    long_v = "длинное содержательное поле §8 разбора идеи — наблюдение, не совет купить; " * 6  # ~440 симв.
    поля = {f"{i}. поле §8": long_v for i in range(1, 13)}     # поля 1..12 длинные
    поля["13. Рамка-дисклеймер"] = ""                          # LLM вернул поле13 пустым
    deep = {"поля": поля}
    proto = {"run_id": "ef_20260629T090001Z", "ts": "2026-06-29T09:00:00+00:00", "mode": "live",
             "граф_отбор": {"топ_k": [node], "money_трек": [node],
                            "суд_money": {"CLF.US": {"исход": "УСТОЯЛА", "отчёт_§8": deep}},
                            "треки": {"money": 1, "провизорный": 0}},
             "картограф_идеи": []}
    cards = R.money_ideas_from_protocol(proto)
    pres = {"mode": "long", "still_unclear": []}               # боевой режим: ℹ️ у всех 13 полей
    text = R.format_report(proto, cards[0], pres)
    assert "…(полный отчёт" in text          # тело реально усечено (путь обрезки задействован)
    assert R.RESEARCH_FRAME in text          # но рамка §16 ВЫЖИЛА (футер дописан после усечения)
    assert "(§16)" in text
    assert "не инвестиционная рекомендация" in text
