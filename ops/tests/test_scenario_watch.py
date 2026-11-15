# -*- coding: utf-8 -*-
"""Сторож сценариев (Q-сторож-сценариев, владелец 15.11). Герметично: новости и цены — стабы, сети нет."""
import datetime
import json
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import scenario_watch as W     # noqa: E402

TODAY = datetime.date(2026, 11, 14)


def _rows(n=90, start=100.0, jump=None, sd=0.01):
    """Закрытия: качка ±sd через день, в последние 3 дня — рывок jump (доля)."""
    d0 = datetime.date(2026, 7, 1)
    rows, px = [], start
    for i in range(n):
        px *= (1 + sd) if i % 2 else (1 - sd)
        rows.append([(d0 + datetime.timedelta(days=i)).isoformat(), round(px, 4)])
    if jump:
        for k in range(3):
            rows[-3 + k][1] = round(rows[-4][1] * (1 + jump * (k + 1) / 3), 4)
    return rows


def _counts(spike_today=None, norm=100):
    c = {(TODAY - datetime.timedelta(days=i)).isoformat(): norm for i in range(11)}
    if spike_today is not None:
        c[TODAY.isoformat()] = spike_today
    return c


EPISODES = {
    "e1": {"id": "e1", "trigger_date": "2019-06-13", "title_ru": "атака 1", "sources": [{"url": "https://x/1"}],
           "links": [{"role_tag": "oil", "today_symbol": "BNO", "why_ru": "риск для пути нефти",
                      "prices": {"есть": True, "ход_20": math.log(1.10), "тикер": "BZ=F"}}]},
    "e2": {"id": "e2", "trigger_date": "2011-12-27", "title_ru": "угроза 2", "sources": ["https://x/2"],
           "links": [{"role_tag": "oil", "today_symbol": "BNO", "prices": {"есть": True, "ход_20": math.log(1.06)}}]},
}


def _data(approved=True, rule="любой"):
    return {"горизонт_дней": 20, "пауза_дней": 14, "заготовки": [{
        "id": "t1", "название": "угроза проливу", "что_случилось": "нападают на танкеры", "механизм": "m",
        "эпизоды": ["e1", "e2"], "утверждён": approved,
        "триггер": {"новости": {"слова": ["a", "b"], "всплеск_кратно": 2.0, "мин_статей": 300},
                    "цена": {"бумага": "BNO.US", "дней": 3, "ход_проц": 5}, "правило": rule},
        "ставки": [{"бумага": "BNO.US", "имя": "United States Brent Oil Fund", "роль": "фонд на нефть",
                    "роли": ["oil"], "направление": "up"}],
        "что_опровергнет": ["атаки прекращаются"]}]}


def test_news_signal_needs_spike_floor_and_baseline():
    assert W.news_signal(_counts(450), TODAY, {"всплеск_кратно": 2, "мин_статей": 300})["сработал"]
    assert not W.news_signal(_counts(180), TODAY, {"всплеск_кратно": 2, "мин_статей": 100})["сработал"]   # ×1.8 < ×2
    assert not W.news_signal(_counts(150, norm=50), TODAY, {"всплеск_кратно": 2, "мин_статей": 300})["сработал"]
    thin = {TODAY.isoformat(): 900}                                     # нормы нет — всплеск не считаем
    r = W.news_signal(thin, TODAY, {"всплеск_кратно": 2, "мин_статей": 10})
    assert not r["сработал"] and "обычном уровне" in r["причина"]


def test_price_signal_requires_unusual_move_for_the_stock():
    calm = W.price_signal(_rows(jump=0.08, sd=0.005), {"дней": 3, "ход_проц": 5})
    assert calm["сработал"] and calm["сигм"] >= 2.5
    wild = W.price_signal(_rows(jump=0.08, sd=0.05), {"дней": 3, "ход_проц": 5})   # для дёрганой бумаги +8% — рядовое
    assert not wild["сработал"] and wild["ход_проц"] >= 5
    down = W.price_signal(_rows(jump=-0.10, sd=0.005), {"дней": 3, "ход_проц": -8})
    assert down["сработал"]


def test_draft_is_logged_not_sent_and_approved_sent_once(tmp_path):
    snap = {"BNO.US": _rows(jump=0.0, sd=0.005)}
    kw = dict(episodes=EPISODES, snapshot=snap, news_fn=lambda w, d: _counts(600), live_fn=False,
              fires_path=tmp_path / "f.jsonl", notices_path=tmp_path / "n.jsonl")
    now = datetime.datetime(2026, 11, 14, 12, tzinfo=datetime.timezone.utc)
    r = W.run(now=now, write=True, data=_data(approved=False), **kw)
    assert len(r["сработали"]) == 1 and not (tmp_path / "n.jsonl").exists()
    assert r["тексты"][0].startswith("[Черновик")
    (tmp_path / "f.jsonl").unlink()
    r1 = W.run(now=now, write=True, data=_data(approved=True), **kw)
    r2 = W.run(now=now + datetime.timedelta(hours=6), write=True, data=_data(approved=True), **kw)
    assert len(r1["сработали"]) == 1 and not r2["сработали"]               # пауза 14 дней
    notes = [json.loads(x) for x in (tmp_path / "n.jsonl").read_text("utf-8").splitlines()]
    assert len(notes) == 1 and notes[0]["source"] == "scenario_watch"
    t = notes[0]["text"]
    assert "статей на эту тему за день — 600, обычно около 100" in t
    assert "13 июня 2019 — +10%" in t and "27 декабря 2011 — +6%" in t        # прошлые случаи кодом
    assert "Ставка $500" in t and "атаки прекращаются" in t and "не инвестиционная рекомендация" in t
    rec = json.loads((tmp_path / "f.jsonl").read_text("utf-8").splitlines()[0])
    assert rec["отправлено"] and rec["ставки"][0]["ход"]["n"] == 2


def test_rule_both_needs_news_and_price():
    snap = {"BNO.US": _rows(jump=0.0, sd=0.005)}
    r = W.run(now=datetime.datetime(2026, 11, 14, 12, tzinfo=datetime.timezone.utc), write=False,
              data=_data(rule="оба"), episodes=EPISODES, snapshot=snap, news_fn=lambda w, d: _counts(600),
              live_fn=False, fires_path="/nonexistent/f.jsonl")
    assert not r["сработали"]


def test_one_past_case_gives_no_bet_sheet():
    data = _data()
    data["заготовки"][0]["эпизоды"] = ["e1"]
    snap = {"BNO.US": _rows(sd=0.005)}
    text, bets = W.render(data["заготовки"][0], {"новости": {"сработал": True, "сейчас": 600, "норма": 100,
                                                             "кратно": 6.0, "день": "2026-09-27"}, "цена": None},
                          EPISODES, snap)
    assert "ложной точностью" in text and bets[0]["ставка"] is None


def test_real_templates_validate():
    assert W.validate() == []
