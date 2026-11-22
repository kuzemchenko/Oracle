# -*- coding: utf-8 -*-
"""Q-цена-страха (22.11): датчик «защита дорогая или дешёвая» — расчёт кодом (Инв#6), без сети и базы."""
import math
import pathlib
import random
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import fear_gauge as FG   # noqa: E402


def _series(n, daily_sd=0.01, seed=7, start=100.0):
    """n цен: дневные доходности — нормальные с заданным разбросом (детерминированно по seed)."""
    rnd = random.Random(seed)
    v = [start]
    for _ in range(n - 1):
        v.append(v[-1] * math.exp(rnd.gauss(0.0, daily_sd)))
    return v


def _dated(vals):
    return [(f"d{i:04d}", v) for i, v in enumerate(vals)]


# ── фактические колебания ──
def test_realized_vol_matches_hand_calculation():
    # доходности строго чередуются +1% / −1% (логарифмические): среднее 0, выборочное отклонение = 0.01·√(n/(n−1))
    v = [100.0]
    for i in range(20):
        v.append(v[-1] * math.exp(0.01 if i % 2 == 0 else -0.01))
    want = 0.01 * math.sqrt(20 / 19) * math.sqrt(252)
    assert FG.realized_vol(v, 20) == pytest.approx(want, rel=1e-12)
    assert FG.realized_vol(_dated(v), 20) == pytest.approx(want, rel=1e-12)      # пары (дата, цена) тоже


def test_realized_vol_uses_only_last_window_and_refuses_short_history():
    calm, wild = _series(300, 0.002, seed=1), _series(22, 0.03, seed=2, start=50.0)
    v = calm + [calm[-1] * x / wild[0] for x in wild[1:]]            # тихий год, потом бурный месяц
    assert FG.realized_vol(v, 21) > 5 * FG.realized_vol(calm, 250)
    assert FG.realized_vol([100.0] * 30, 21) == 0.0                  # цена не менялась — колебаний нет
    assert FG.realized_vol([100.0] * 21, 21) is None                 # 21 цена = 20 доходностей — мало
    assert FG.realized_vol([], 21) is None and FG.realized_vol(None, 21) is None
    assert FG.realized_vol([100.0, None, "x", -5, 101.0], 1) is None     # мусор отброшен, окна меньше двух дней нет


def test_measures_month_and_three_years():
    v = _dated(_series(900, 0.01))
    ms = FG.measures(v)
    assert ms["дней_обычных"] == 756 and ms["дата"] == "d0899"
    assert 0.13 < ms["обычные"] < 0.19 and 0.08 < ms["за_месяц"] < 0.25      # 1% в день ≈ 16% годовых
    assert ms["обычные"] == round(FG.realized_vol(v, 756), 4) and ms["за_месяц"] == round(FG.realized_vol(v, 21), 4)


def test_measures_short_history_is_honest():
    ms = FG.measures(_dated(_series(600, 0.01)))             # 2.4 года: считаем по всей истории и говорим, сколько дней
    assert ms["дней_обычных"] == 599 and ms["обычные"] is not None
    ms = FG.measures(_dated(_series(300, 0.01)))             # год с небольшим — «обычные» не считаем
    assert ms["обычные"] is None and ms["дней_обычных"] is None and ms["за_месяц"] is not None
    ms = FG.measures(_dated(_series(10, 0.01)))
    assert ms["обычные"] is None and ms["за_месяц"] is None
    assert FG.measures(None) == {"за_месяц": None, "обычные": None, "дней_обычных": None, "дата": None}


# ── вердикт ──
def test_verdict_on_owner_numbers_22_nov():
    v, why = FG.verdict(0.36, 0.16, 0.26)                    # замер 22.11: 36% против обычных 16% и 26% за месяц
    assert v == FG.EXPENSIVE
    assert "в 2.2 раза больше обычных (36% против 16%)" in why[0] and "наценка за страх" in why[1] and "26%" in why[1]


def test_verdict_thresholds():
    assert FG.verdict(0.24, 0.16)[0] == FG.EXPENSIVE         # ровно в полтора раза — уже дорого
    assert FG.verdict(0.2399, 0.16)[0] == FG.NORMAL
    assert FG.verdict(0.20, 0.16, 0.15)[0] == FG.NORMAL
    assert FG.verdict(0.16, 0.16)[0] == FG.CHEAP             # не больше обычных — дёшево
    assert FG.verdict(0.1601, 0.16)[0] == FG.NORMAL
    assert FG.verdict(0.12, 0.16, 0.08)[0] == FG.CHEAP       # спокойный рынок: опционы дешевле обычного уровня


def test_verdict_stays_expensive_in_the_middle_of_a_crash():
    # рынок за месяц колебался сильнее (45%), чем закладывают опционы (36%), — защита всё равно кратно дороже обычной
    v, why = FG.verdict(0.36, 0.16, 0.45)
    assert v == FG.EXPENSIVE and "уже случившейся встряски" in why[1] and "45%" in why[1]
    assert "примерно столько" in FG.verdict(0.27, 0.16, 0.26)[1][1]


def test_no_options_data_means_not_measured_but_realized_is_returned():
    closes = _dated(_series(900, 0.01))
    g = FG.gauge(closes, None)
    assert g["вердикт"] is None and g["закладывают"] is None and g["к_обычным"] is None
    assert g["обычные"] is not None and g["за_месяц"] is not None and "не измерена" in g["почему"][0]
    for junk in (5.0, -0.2, 0.0, "нет", float("nan")):       # мусор от поставщика — не измерение
        assert FG.gauge(closes, junk)["вердикт"] is None and FG.gauge(closes, junk)["закладывают"] is None
    g = FG.gauge(_dated(_series(300, 0.01)), 0.30)           # опционы есть, истории мало — тоже «не измерено»
    assert g["вердикт"] is None and g["закладывают"] == 0.3 and "мало истории" in g["почему"][0]


def test_gauge_ratios_and_thresholds_in_result():
    closes = _dated(_series(900, 0.01))
    us, rc = FG.measures(closes)["обычные"], FG.measures(closes)["за_месяц"]
    g = FG.gauge(closes, us * 2)
    assert g["вердикт"] == FG.EXPENSIVE and g["к_обычным"] == 2.0 and g["к_недавним"] == round(us * 2 / rc, 2)
    assert g["пороги"] == {"дорогая_от": 1.5, "дешёвая_до": 1.0}
    assert FG.gauge(closes, us * 0.9)["вердикт"] == FG.CHEAP and FG.gauge(closes, us * 1.2)["вердикт"] == FG.NORMAL


# ── слова ──
def _g(iv, us, rc, exp=None):
    v, why = FG.verdict(iv, us, rc)
    return {"закладывают": iv, "обычные": us, "за_месяц": rc, "вердикт": v, "почему": why,
            "опционы": {"экспирация": exp} if exp else None}


def test_phrase_plain_words_on_owner_numbers():
    assert FG.phrase(_g(0.36, 0.16, 0.26)) == (
        "Цена защиты: дорогая — опционы на S&P 500 (фонд SPY) закладывают 36% годовых колебаний при фактических 26% "
        "за месяц и обычных 16%; защиту от падения (опцион «пут») и ставки на падение через опционы лучше покупать, "
        "когда она дешёвая, а не после всплеска страха; это цена опционов, а не довод против самой идеи на падение — "
        "её можно выразить и без опционов.")
    s = FG.phrase(_g(0.14, 0.16, 0.12, exp="2026-12-24"))
    assert s.startswith("Цена защиты: дешёвая — ") and "(на срок до 2026-12-24)" in s and "страх дёшев" in s
    assert "ставки на падение через опционы выгоднее покупать сейчас" in s
    s = FG.phrase(_g(0.20, 0.16, None))
    assert "обычная" in s and "за месяц" not in s and "при обычных 16%" in s


def test_phrase_speaks_about_option_price_not_against_downside_ideas():
    """Ревью 22.11: фраза идёт в сводку агента сразу после «рассмотри, кто проигрывает (идея на падение)». Дорогая
    защита — довод против покупки ОПЦИОНОВ, а не против идеи на падение (владелец 22.11 просил такие идеи)."""
    for g in (_g(0.36, 0.16, 0.26), _g(0.14, 0.16, 0.12)):
        s = FG.phrase(g)
        assert "ставки на падение через опционы" in s
        assert "ставки на падение лучше" not in s and "идеи на падение выгоднее" not in s
    assert "не довод против самой идеи на падение" in FG.phrase(_g(0.36, 0.16, 0.26))


def test_phrase_is_absent_without_measurement():
    g = FG.gauge(_dated(_series(900, 0.01)), None)
    assert FG.phrase(g) is None and FG.phrase(None) is None and FG.phrase({}) is None
    s = FG.phrase_unmeasured(dict(g, не_измерено="опционы не получены (EODHD ответил 401)"))
    assert s.startswith("Цена защиты: не измерена — опционы не получены (EODHD ответил 401).")
    assert "Фактические годовые колебания S&P 500 (фонд SPY):" in s and "за месяц" in s and "обычно" in s
    assert FG.phrase_unmeasured(_g(0.36, 0.16, 0.26)) is None        # измерено — «не измерена» не пишем
    s = FG.phrase_unmeasured(g)                                      # причина из «почему» — без повтора «не измерена»
    assert s.startswith("Цена защиты: не измерена — нет данных о том, какие колебания закладывают опционы. Фактические")
    assert s.count("не измерена") == 1
