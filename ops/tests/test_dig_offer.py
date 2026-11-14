# -*- coding: utf-8 -*-
"""Тесты «что покопать» (владелец 14.11): код держит П8 (повод только из меню, ссылки ставит код),
тип «почему рынок не видит», круги, повторы; бот шлёт одно сообщение с кнопками, «Копаем» уходит агенту
запросом «найди идею», «Мимо» записывается и тему больше не предлагают. Герметично: без сети и модели."""
import datetime
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import dig_offer as D        # noqa: E402
import bot_state as S        # noqa: E402
import bot as BOT            # noqa: E402

NOW = datetime.datetime(2026, 11, 14, 5, 0, tzinfo=datetime.timezone.utc)
MENU = {"собрано": "2026-11-13T21:00:19+00:00",
        "newsapi": {"события": [{"событие": "Microsoft's Global Outage", "статей": 3249, "дата": "2026-11-13",
                                 "uri": "eng-1"}]},
        "gdelt": {"резко_выросло": [{"что": "MICROSOFT", "статей_сутки": 5506, "рост_раз": 7.0,
                                      "примеры": ["https://example.com/ms-outage"]}]},
        "wiki": {"резко_выросло": [{"статья": "Colette Kress", "просмотров_вчера": 463931, "рост_раз": 464.9}]}}


def _offer(**kw):
    o = {"тема": "Сбой облака Microsoft и страховщики киберрисков", "повод_ids": ["N1", "G1"],
         "что_случилось": "Глобальный сбой сервисов Microsoft на сутки.",
         "круги": ["1-й круг: сама Microsoft", "2-й круг: конкуренты облака", "3-й круг: страховщики киберрисков"],
         "почему_дальше": "Крупные клиенты пересмотрят зависимость от одного поставщика.",
         "тип": "дата", "почему_рынок_не_видит": "Продления контрактов приходятся на январь.",
         "что_опровергнет": "Если клиенты не заговорят о втором поставщике до конца года.",
         "аналог_id": None}
    o.update(kw)
    return o


class FakeClient:
    mode = "mock"

    def __init__(self, payload):
        self.payload = payload

    def complete(self, role, system, user, **kw):
        assert role == "dig_scout" and "[N1]" in user
        return {"text": json.dumps(self.payload, ensure_ascii=False), "model": "fake", "cost": 0.0}


def _run(tmp_path, payload, **kw):
    kw.setdefault("episodes", [])
    return D.run(mode="mock", now=NOW, client=FakeClient(payload), menu=MENU, attention_daily={},
                 attention_fast={}, out_dir=tmp_path / "dig", decisions=tmp_path / "dec.jsonl",
                 search_dir=tmp_path / "s", **kw)


def test_good_offer_passes_and_sources_come_from_code(tmp_path):
    p, pr = _run(tmp_path, {"предложения": [_offer()]})
    assert p.exists() and len(pr["предложения"]) == 1
    o = pr["предложения"][0]
    assert [s["url"] for s in o["источники"]] == ["https://eventregistry.org/event/eng-1", "https://example.com/ms-outage"]
    assert pr["текст"] and "Копаем N" in pr["текст"] and "не инвестиционная рекомендация" in pr["текст"]
    assert "похожего случая в нашей библиотеке нет" in pr["текст"]


@pytest.mark.parametrize("bad,why", [
    ({"повод_ids": ["N9"]}, "не из меню"),
    ({"повод_ids": []}, "повод не указан"),
    ({"тип": "интуиция"}, "тип"),
    ({"круги": ["только сама новость"]}, "меньше двух кругов"),
    ({"что_опровергнет": ""}, "что_опровергнет"),
])
def test_bad_offers_rejected(tmp_path, bad, why):
    _, pr = _run(tmp_path, {"предложения": [_offer(**bad)]})
    assert not pr["предложения"] and why in pr["брак"][0]["причина"]


def test_repeat_of_recent_topic_rejected(tmp_path):
    (tmp_path / "dec.jsonl").write_text(json.dumps({"ts": "2026-11-10T06:00:00+00:00", "решение": "мимо",
                                                    "тема": "Сбой облака Microsoft и страховщики"},
                                                   ensure_ascii=False) + "\n", encoding="utf-8")
    _, pr = _run(tmp_path, {"предложения": [_offer()]})
    assert not pr["предложения"] and "повтор" in pr["брак"][0]["причина"]


def test_stale_menu_is_honest_refusal(tmp_path):
    old = dict(MENU, собрано="2026-11-10T21:00:00+00:00")
    _, pr = D.run(mode="mock", now=NOW, client=FakeClient({}), menu=old, attention_daily={}, attention_fast={},
                  out_dir=tmp_path / "dig", decisions=tmp_path / "d", search_dir=tmp_path / "s")
    assert pr["ОТКАЗ"] and "ничего не предлагаю" in pr["текст"]


def test_nothing_to_dig_is_legit(tmp_path):
    _, pr = _run(tmp_path, {"предложения": [], "почему_ничего": "поводы дня без экономики"})
    assert "ничего не предлагаю" in pr["текст"] and "без экономики" in pr["текст"]


def test_bare_ticker_fails_lint(tmp_path):
    _, pr = _run(tmp_path, {"предложения": [_offer(почему_дальше="Выиграет CRWD.US раньше всех.")]})
    assert pr["текст"] is None and any("тикеры" in v for v in pr["линт"])


def test_analog_by_library_id_filled_by_code(tmp_path):
    ep = {"id": "2024_crowdstrike_outage", "trigger_date": "2024-07-19", "title_ru": "Сбой CrowdStrike",
          "counter_case_ru": "Акции быстро отыграли.", "sources": [{"url": "https://example.com/cs"}]}
    _, pr = _run(tmp_path, {"предложения": [_offer(аналог_id="2024_crowdstrike_outage")]}, episodes=[ep])
    assert "«Сбой CrowdStrike» (2024)" in pr["текст"] and pr["предложения"][0]["аналог"]["url"] == "https://example.com/cs"


def test_unknown_analog_id_means_no_analog(tmp_path):
    _, pr = _run(tmp_path, {"предложения": [_offer(аналог_id="выдуманный_случай")]})
    o = pr["предложения"][0]
    assert o["аналог"] is None and "не из библиотеки" in o["аналог_брак"]
    assert "похожего случая в нашей библиотеке нет" in pr["текст"]


def test_too_long_drops_last_offer(tmp_path):
    long = "Очень длинное объяснение без жаргона. " * 6
    names = ["Медь и стройка в Индии", "Танкеры после санкций", "Удобрения и засуха в Бразилии"]
    offers = [_offer(тема=names[n - 1], почему_дальше=long[:250], что_случилось=long[:250],
                     почему_рынок_не_видит=long[:250], что_опровергнет=long[:250],
                     круги=[long[:240], long[:240], long[:240]]) for n in range(1, 4)]
    _, pr = _run(tmp_path, {"предложения": offers})
    assert len(pr["текст"]) <= D.MAX_LEN and len(pr["предложения"]) < 3
    assert any("не влезла" in b["причина"] for b in pr["брак"])


# ── бот ─────────────────────────────────────────────────────────────────────────────────
class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.edits, self._mid = [], [], [], 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._mid

    def edit_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append(reply_markup)
        return {}

    def answer_callback(self, cb_id, text=None, show_alert=False):
        self.answers.append(text)
        return {}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")
    monkeypatch.setattr(BOT, "DELIVERIES_PATH", tmp_path / "deliveries.jsonl")
    monkeypatch.setattr(BOT.Bot, "DIG_DIR", tmp_path / "dig")
    monkeypatch.setattr(BOT.Bot, "DIG_DECISIONS", tmp_path / "dig_decisions.jsonl")
    monkeypatch.setattr(BOT.R, "load_presentation", lambda: {"dig_offer_push": True})
    monkeypatch.setattr(S, "now_utc", lambda: NOW + datetime.timedelta(hours=1))
    b = BOT.Bot(FakeTelegram(), "555", state=S._default_state())
    b.asked = []
    monkeypatch.setattr(b, "_chat", lambda chat, text, attachments=None: b.asked.append(text))
    return b


def test_bot_pushes_with_buttons_once_and_kopaem_goes_to_agent(tmp_path, bot):
    p, _ = _run(tmp_path, {"предложения": [_offer(), _offer(тема="Вторая ветка про чипы и энергию", повод_ids=["W1"])]})
    bot._tick_dig_offer()
    bot._tick_dig_offer()                                     # водяной знак — второй раз не шлёт
    assert len(bot.tg.sent) == 1
    kb = bot.tg.sent[0][2]["inline_keyboard"]
    assert [b["text"] for b in kb[0]] == ["Копаем 1", "Копаем 2"] and kb[1][0]["callback_data"] == f"g:x:{p.stem}"
    bot._on_callback({"id": "c", "data": f"g:2:{p.stem}", "message": {"message_id": 1, "chat": {"id": 555}}})
    assert bot.asked and bot.asked[0].startswith("Найди идею по теме «Вторая ветка")
    recs = [json.loads(x) for x in (tmp_path / "dig_decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["решение"] for r in recs] == ["не выбрано", "копаем"]
    bot._on_callback({"id": "c", "data": f"g:1:{p.stem}", "message": {"message_id": 1, "chat": {"id": 555}}})
    assert len(bot.asked) == 1 and "уже решено" in bot.tg.answers[-1]


def test_bot_mimo_records_and_no_agent(tmp_path, bot):
    p, _ = _run(tmp_path, {"предложения": [_offer()]})
    bot._tick_dig_offer()
    bot._on_callback({"id": "c", "data": f"g:x:{p.stem}", "message": {"message_id": 1, "chat": {"id": 555}}})
    assert not bot.asked
    rec = json.loads((tmp_path / "dig_decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["решение"] == "мимо" and rec["тема"].startswith("Сбой облака")


def test_bot_lint_failed_protocol_not_sent(tmp_path, bot, monkeypatch):
    notes = []
    monkeypatch.setattr(bot, "_notice_owner", lambda t: notes.append(t))
    _run(tmp_path, {"предложения": [_offer(почему_дальше="Выиграет CRWD.US раньше всех.")]})
    bot._tick_dig_offer()
    assert not bot.tg.sent and notes


def test_bot_skips_stale_protocol(tmp_path, bot, monkeypatch):
    _run(tmp_path, {"предложения": [_offer()]})
    monkeypatch.setattr(S, "now_utc", lambda: NOW + datetime.timedelta(hours=30))
    bot._tick_dig_offer()
    assert not bot.tg.sent and bot.state.get("dig_mark")


def test_real_presentation_loader_passes_dig_flags():
    """Регресс 14.11: загрузчик подачи пропускает только известные ключи — новые флаги терялись."""
    import bot_reports as RR
    pres = RR.load_presentation()
    assert "dig_offer_push" in pres and "dig_offer_auto" in pres
    names = [j[0] for j in BOT.SCHEDULED_JOBS]
    assert "dig_offer_auto" in names
