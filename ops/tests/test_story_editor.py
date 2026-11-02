# -*- coding: utf-8 -*-
"""Тесты редактора-рассказчика (ops/story_editor.py, вердикт владельца 02.11).

Закрепляют: хороший пересказ (те же числа/тикеры, секции, рамка) принят; выдуманное
число / чужой тикер / потерянная рамка / жаргон / эмодзи — ОТКАЗ кодом; недоступный
клиент — мягкий отказ (None, причина); нормализация чисел (0,5 = 0.50, 07 = 7);
бот: флаг включён + рассказ прошёл → уходит рассказ, отказ → строгий формат + дозор."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import story_editor as SE     # noqa: E402

SRC = """Наблюдение дня — 2 ноября. Cheniere (СПГ) (LNG.US): возможный рост.

Что это значит для тебя. Сегодня защищать нечего — показываю наблюдение.

Потенциал. Ход около 3.2% вверх, шанс около 48%, срок около недели.

Что делать. Скажи «что ещё было». Решение за тобой.
Это исследовательский инструмент, не инвестиционная рекомендация."""

GOOD = """Наблюдение дня — 2 ноября. Cheniere (СПГ) (LNG.US): возможный рост.

Что это значит для тебя. В мире подорожал газ, и главный американский экспортёр
сжиженного газа может на этом заработать — но пока это наблюдение, не ставка.

Если смотреть на деньги: ход около 3.2% вверх с шансом около 48%, горизонт — около недели.

Что делать. Ничего. Хочешь глубже — скажи «что ещё было». Решение за тобой.
Это исследовательский инструмент, не инвестиционная рекомендация."""


class StubClient:
    def __init__(self, text):
        self.text = text

    def complete(self, role, system, user, *, agent_id, output_kind):
        return {"text": self.text, "model": "stub", "cost": 0.0}


def test_good_story_accepted():
    story, why = SE.rewrite(SRC, client=StubClient(GOOD))
    assert why is None and "подорожал газ" in story


def test_invented_number_rejected():
    bad = GOOD.replace("около 48%", "около 99%")
    story, why = SE.rewrite(SRC, client=StubClient(bad))
    assert story is None and "числа не из входа" in why and "99" in why


def test_foreign_ticker_rejected():
    bad = GOOD.replace("(LNG.US)", "(LNG.US), а ещё Nvidia (NVDA.US)")
    story, why = SE.rewrite(SRC, client=StubClient(bad))
    assert story is None and "тикеры не из входа" in why


def test_lost_frame_and_lint_rejected():
    story, why = SE.rewrite(SRC, client=StubClient(
        GOOD.replace("Это исследовательский инструмент, не инвестиционная рекомендация.", "")))
    assert story is None and "рамка" in why
    story2, why2 = SE.rewrite(SRC, client=StubClient(GOOD.replace("возможный рост.", "возможный рост 🚀.")))
    assert story2 is None and ("эмодзи" in why2 or "пиктограм" in why2)


def test_number_normalization_tolerant():
    # 3.2 ↔ 3,20 и 48 ↔ 048 — одно и то же число, не повод для отказа
    ok = GOOD.replace("около 3.2%", "около 3,20%")
    story, why = SE.rewrite(SRC, client=StubClient(ok))
    assert why is None, why


def test_client_failure_is_soft():
    class Boom:
        def complete(self, *a, **k):
            raise RuntimeError("нет ключа")
    story, why = SE.rewrite(SRC, client=Boom())
    assert story is None and "редактор недоступен" in why


def test_bare_hallucinated_ticker_rejected():
    """Ре-гейт №2 02.11: промпт v2 велит писать тикер ГОЛЫМ — гард обязан ловить и голую
    форму, иначе он вакуумный (ловил только .US, которого по промпту не бывает)."""
    bad = GOOD.replace("возможный рост.", "возможный рост, а ещё выиграет XOM.")
    story, why = SE.rewrite(SRC, client=StubClient(bad))
    assert story is None and "тикеры не из входа" in why and "XOM" in why


def test_bare_ticker_of_source_paper_accepted():
    src = SRC.replace("Cheniere (СПГ) (LNG.US)", "Nvidia (NVDA.US)")
    good = (GOOD.replace("Cheniere (СПГ) (LNG.US)", "Nvidia (NVDA.US)")
            .replace("скажи «что ещё было»", "скажи «разбери NVDA»"))
    story, why = SE.rewrite(src, client=StubClient(good))
    assert why is None, why


def test_non_us_suffix_ticker_rejected():
    bad = GOOD.replace("возможный рост.", "возможный рост, как и S&P 500 (GSPC.INDX).")
    story, why = SE.rewrite(SRC, client=StubClient(bad))
    assert story is None and "тикеры не из входа" in why


def test_sign_flip_rejected():
    """Знак — часть числа: «−0.4» во входе ≠ «0.4» в рассказе (переворот матожидания)."""
    src = SRC.replace("Ход около 3.2% вверх", "Матожидание около −0.4%")
    bad = GOOD.replace("ход около 3.2% вверх с шансом около 48%",
                       "выигрыш около 0.4% с шансом около 48%")
    story, why = SE.rewrite(src, client=StubClient(bad))
    assert story is None and "числа не из входа" in why


def test_verbal_quantities_not_from_source_rejected():
    """Перепроверка №2 02.11: числа ПРОПИСЬЮ обходили цифровой гард — «в девяти случаях
    из десяти» без опоры во входе теперь брак."""
    bad = GOOD.replace("с шансом около 48%", "— сбывается в девяти случаях из десяти")
    story, why = SE.rewrite(SRC, client=StubClient(bad))
    assert story is None and "количественные слова" in why


def test_minus_in_words_matches_signed_source():
    src = SRC.replace("Ход около 3.2% вверх", "Матожидание −0.4%")
    good = GOOD.replace("ход около 3.2% вверх с шансом около 48%",
                        "итог минус 0.4% при шансе около 48%")
    story, why = SE.rewrite(src, client=StubClient(good))
    assert why is None, why


def test_hyphen_range_not_sign():
    src = SRC.replace("срок около недели", "срок 3-5 недель")
    good = GOOD.replace("горизонт — около недели", "горизонт — около 5 недель")
    story, why = SE.rewrite(src, client=StubClient(good))
    assert why is None, why


def _bot_tick_with_story(monkeypatch, rewrite_fn, tmp_path):
    from test_bot_v2_loop import _bot
    import bot as BOT
    import bot_state as S
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(S, "DECISIONS_PATH", tmp_path / "decisions_user.jsonl")
    monkeypatch.setattr(BOT, "ROOT", tmp_path)
    proto = {"run_id": "ef_s", "ts": "2026-11-02T09:00:00+00:00", "mode": "live"}
    monkeypatch.setattr(BOT.R, "load_presentation",
                        lambda: {"send_mock_to_telegram": False, "idea_day_lead": True,
                                 "top3_lead": False, "daily_case_lead": False,
                                 "story_editor": True, "hunter_push": False,
                                 "mode": "short", "still_unclear": set()})
    monkeypatch.setattr(BOT.R, "new_runs", lambda pushed: [proto])
    monkeypatch.setattr(BOT.R, "idea_of_day_from_protocol",
                        lambda p, **kw: ("Идея дня — строгий текст.",
                                         {"idx": {"1": "LNG.US"}, "lead": "LNG.US"}, []))
    monkeypatch.setattr(BOT.R, "money_ideas_from_protocol", lambda p: [])
    monkeypatch.setattr(BOT.W, "ingest_protocol", lambda p, existing_ids=None: [])
    monkeypatch.setattr(SE, "rewrite", rewrite_fn)
    b = _bot()
    notes = []
    monkeypatch.setattr(b, "_notice_dozor", lambda t: notes.append(t))
    b._tick_reports()
    return b, notes


def test_bot_story_push_keeps_v2_buttons(monkeypatch, tmp_path):
    """Ре-гейт №2 02.11 (регрессия high): рассказ — тот же дневной лид и уходит С КНОПКАМИ
    [Качаем 2 дня][Что ещё было][Мимо]; иначе включение флага молча убивает петлю §12."""
    calls = []
    b, _ = _bot_tick_with_story(
        monkeypatch, lambda text, **k: calls.append(1) or ("Рассказ для владельца.", None),
        tmp_path)
    assert b.tg.sent, "пуш не ушёл"
    _, text, kb = b.tg.sent[-1]
    assert "Рассказ" in text and calls == [1]
    labels = [btn["text"] for row in (kb or {}).get("inline_keyboard", []) for btn in row]
    assert any("Качаем" in x for x in labels) and any("Мимо" in x for x in labels)


def test_bot_story_refusal_falls_back_to_strict_with_dozor(monkeypatch, tmp_path):
    b, notes = _bot_tick_with_story(
        monkeypatch, lambda text, **k: (None, "числа не из входа (П8): 99"), tmp_path)
    _, text, kb = b.tg.sent[-1]
    assert "строгий текст" in text                      # фолбэк: подача не теряется
    assert notes and "99" in notes[0]                   # заметка в дозор
    labels = [btn["text"] for row in (kb or {}).get("inline_keyboard", []) for btn in row]
    assert any("Качаем" in x for x in labels)           # кнопки и у строгого формата

def test_verbal_quantities_inside_quotes_rejected():
    """Перепроверка №5: кавычки рассказчика — его же текст; «в девяти случаях из десяти»
    внутри «…» без опоры во входе — брак (иначе обход П8/Инв#6)."""
    bad = GOOD.replace("с шансом около 48%", "с шансом около 48% («сбывается в девяти случаях из десяти»)")
    story, why = SE.rewrite(SRC, client=StubClient(bad))
    assert story is None and "количественные слова" in why
