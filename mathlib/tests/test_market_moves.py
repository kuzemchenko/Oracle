# -*- coding: utf-8 -*-
"""mathlib/market_moves.py — «резкое движение» цены против собственной истории (Q-меню-рынок). Герметично: ряды
цен строятся в тесте."""
import datetime
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from mathlib import market_moves as MM   # noqa: E402


def _calm(n=300, step=0.01, start=100.0):
    """Спокойный ряд: день +step, день −step (обычное дневное колебание ≈ step)."""
    v = [start]
    for i in range(n):
        v.append(v[-1] * (1 + (step if i % 2 == 0 else -step)))
    return v


def _extend(v, moves):
    v = list(v)
    for r in moves:
        v.append(v[-1] * (1 + r))
    return v


def _dated(v, end=datetime.date(2026, 11, 20)):
    return [((end - datetime.timedelta(days=len(v) - 1 - i)).isoformat(), c) for i, c in enumerate(v)]


def test_day_move_is_measured_in_typical_daily_swings():
    m = MM.measure(_extend(_calm(), [-0.05]), days=1)
    assert m["ход"] == pytest.approx(-0.05, abs=1e-4)
    assert m["обычное"] == pytest.approx(0.01, abs=5e-4)          # норма — год ДО измеряемого дня
    assert m["кратность"] == pytest.approx(-5.0, abs=0.2)
    assert m["наблюдений"] == 252
    assert MM.is_sharp(m)


def test_week_move_uses_five_day_swings_and_ignores_its_own_window():
    a = MM.measure(_extend(_calm(), [-0.02] * 5), days=5)
    b = MM.measure(_extend(_calm(), [-0.06] * 5), days=5)
    assert a["ход"] == pytest.approx(0.98 ** 5 - 1, abs=1e-4)
    assert a["обычное"] == b["обычное"]                           # сам обвал свою норму не раздувает
    assert abs(b["кратность"]) > abs(a["кратность"]) > 3
    assert a["наблюдений"] == 248                                 # 252 дневных хода → 248 пятидневных окон


def test_ordinary_day_is_not_sharp():
    m = MM.measure(_extend(_calm(), [0.012]), days=1)
    assert abs(m["кратность"]) < 3 and not MM.is_sharp(m)
    sm = MM.summary(_extend(_calm(), [0.012]))
    assert sm["резкое"] is False and sm["окно"] in ("день", "неделя")


def test_quiet_paper_needs_two_percent_floor():
    v = _extend(_calm(step=0.0005), [0.004])                       # 8 обычных колебаний, но всего +0.4%
    m = MM.measure(v, days=1)
    assert abs(m["кратность"]) > 3 and not MM.is_sharp(m)
    assert MM.is_sharp(m, {"мин_ход": 0.003})                      # порог — параметр, не зашит


def test_little_history_is_none_not_a_number():
    assert MM.measure(_extend(_calm(n=60), [-0.05]), days=1) is None
    assert MM.measure([], days=1) is None and MM.measure([100.0], days=1) is None
    assert MM.summary(_calm(n=30)) is None
    assert MM.is_sharp(None) is False


def test_split_gap_in_history_does_not_inflate_the_norm():
    base = _calm()
    with_split = base[:150] + [c * 0.32 for c in base[150:]]       # дробление без поправки: −68% за день
    m0 = MM.measure(_extend(base, [-0.05]), days=1)
    m1 = MM.measure(_extend(with_split, [-0.05]), days=1)
    assert m1["обычное"] == pytest.approx(m0["обычное"], rel=0.02)  # день разрыва в норму не пошёл
    assert MM.is_sharp(m1)


def test_gap_inside_measured_window_is_not_measured():
    v = _extend(_calm(), [0.98, 0.01, -0.01])                      # +98% три дня назад — не рынок
    assert MM.measure(v, days=5) is None
    assert MM.measure(v, days=1) is not None                        # сам последний день — чистый
    assert MM.window_has_gap(v, 5) and not MM.window_has_gap(_calm(), 5)
    sm = MM.summary(v)
    assert sm["неделя"] is None and sm["день"] is not None and sm["резкое"] is False
    assert MM.measure(_extend(_calm(), [-0.6]), days=1) is None     # разрыв в последний день — «не измерено»


def test_accepts_dated_pairs_and_skips_bad_prices():
    v = _extend(_calm(), [-0.05])
    pairs = _dated(v)
    pairs.insert(100, ("2026-01-01", None))
    pairs.insert(120, ("2026-01-02", 0.0))
    m = MM.measure(pairs, days=1)
    assert m["дата"] == "2026-11-20" and m["кратность"] == MM.measure(v, days=1)["кратность"]
    assert MM.measure(v, days=1)["дата"] is None


def test_summary_picks_the_stronger_sharp_window():
    crash = MM.summary(_dated(_extend(_calm(), [-0.015] * 4 + [-0.06])))
    assert crash["резкое"] and crash["вниз"] and crash["дата"] == "2026-11-20"
    assert crash["окно"] == max(("день", "неделя"), key=lambda k: abs(crash[k]["кратность"]))
    assert crash["сила"] == abs(crash[crash["окно"]]["кратность"])
    slow = MM.summary(_extend(_calm(), [-0.02] * 5))               # за день не резкое, за неделю — резкое
    assert not MM.is_sharp(slow["день"]) and slow["резкое"] and slow["окно"] == "неделя"
    up = MM.summary(_extend(_calm(), [0.07]))
    assert up["резкое"] and up["вниз"] is False


def test_window_returns_and_typical_move_basics():
    assert MM.window_returns([0.1, 0.1, None, 0.1], 2) == [pytest.approx(0.21), None, None]
    assert MM.daily_returns([100, 110, 55], gap=0.45) == [pytest.approx(0.1), None]
    assert MM.typical_move([0.01, -0.01] * 10, 1, min_obs=30) is None        # наблюдений меньше минимума
    assert MM.typical_move([0.0] * 200, 1) is None                           # нулевая норма — делить не на что
