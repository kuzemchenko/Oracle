# -*- coding: utf-8 -*-
"""Независимость ставок (mathlib/independence.py): связь бумаг, связь ставок со знаком сторон, число независимых."""
import random

from mathlib import independence as IN


def _series(seed, n=150, base=None, mix=0.0):
    """Ряд цен; mix — доля общего движения с рядом base (1.0 — ходят вместе)."""
    rnd = random.Random(seed)
    own = [rnd.gauss(0, 0.02) for _ in range(n)]
    common = IN.returns(base) if base else {}
    out, px = [], 100.0
    for i in range(n):
        d = "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)
        r = mix * common.get(d, 0.0) + (1 - mix) * own[i]
        px *= 1 + r
        out.append((d, px))
    return out


A = _series(1)
B_SAME = _series(2, base=A, mix=0.95)
C_FREE = _series(3)


def test_correlation_sees_together_and_apart_and_refuses_short_history():
    assert IN.correlation(A, B_SAME) > 0.9 and abs(IN.correlation(A, C_FREE)) < 0.3
    assert IN.correlation(A[:30], B_SAME[:30]) is None                      # мало общих дней — «не знаем», а не «независимы»
    flat = [(d, 100.0) for d, _ in A]
    assert IN.correlation(A, flat) is None


def test_opposite_sides_on_linked_papers_are_a_counterweight_not_a_repeat():
    assert IN.bet_link(0.9, "вверх", "вверх") == 0.9 and IN.bet_link(0.9, "вверх", "вниз") == -0.9
    assert IN.bet_link(None, "вверх", "вниз") is None


def _corr(table):
    return lambda a, b: table.get((a, b), table.get((b, a)))


def test_eight_linked_buys_are_about_one_bet():
    """Ноябрь 2026: восемь покупок, упавших вместе с рынком, — по сути одна ставка."""
    bets = [{"тикер": "T%d" % i, "сторона": "вверх"} for i in range(8)]
    n, avg, pairs = IN.effective_n(bets, lambda a, b: 0.8)
    assert n == 1.2 and avg == 0.8 and pairs == 28
    assert IN.effective_n(bets, lambda a, b: 0.0)[0] == 8.0                  # независимые — восемь ставок
    assert IN.effective_n(bets, lambda a, b: None)[0] is None                # не измерено — не выдумываем
    assert IN.effective_n(bets[:1], lambda a, b: 0.8)[0] == 1.0 and IN.effective_n([], lambda a, b: 0.8)[0] is None


def test_new_idea_is_classified_against_open_bets():
    opened = [{"тикер": "NVDA", "сторона": "вверх", "откуда": "разговор"}, {"тикер": "DHT", "сторона": "вверх", "откуда": "охотник"}]
    corr = _corr({("AMD", "NVDA"): 0.85, ("AMD", "DHT"): 0.1, ("NVDA", "DHT"): 0.2,
                  ("TSM", "NVDA"): 0.55, ("TSM", "DHT"): 0.1, ("XOM", "NVDA"): 0.05, ("XOM", "DHT"): 0.3})
    rep = IN.assess({"тикер": "AMD", "сторона": "вверх"}, opened, corr)
    assert rep["вывод"] == IN.REPEAT and rep["похожие"][0]["тикер"] == "NVDA" and rep["та_же_сторона"] == 2
    assert rep["независимых_с_новой"] < 3 and rep["ставок_с_новой"] == 3
    assert IN.assess({"тикер": "TSM", "сторона": "вверх"}, opened, corr)["вывод"] == IN.PARTIAL
    assert IN.assess({"тикер": "XOM", "сторона": "вверх"}, opened, corr)["вывод"] == IN.NEW
    assert IN.assess({"тикер": "AMD", "сторона": "вниз"}, opened, corr)["вывод"] == IN.NEW        # ставка вниз — противовес
    assert IN.assess({"тикер": "NVDA", "сторона": "вверх"}, opened, corr)["вывод"] == IN.SAME     # та же ставка, не повтор
    assert IN.assess({"тикер": "ZZZ", "сторона": "вверх"}, opened, corr)["вывод"] == IN.UNKNOWN
    assert IN.assess({"тикер": "AMD", "сторона": "вверх"}, [], corr)["вывод"] == IN.NEW
