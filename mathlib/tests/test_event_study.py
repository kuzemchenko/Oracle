# -*- coding: utf-8 -*-
"""Проверка исторических каскадов ценами (mathlib/event_study.py)."""
import datetime
import math
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from mathlib import event_study as ES   # noqa: E402


def _dates(n, start="2001-01-01"):
    d0 = datetime.date.fromisoformat(start)
    out, d = [], d0
    while len(out) < n:
        if d.isoweekday() <= 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _walk(n, sd=0.01, seed=1, start=100.0):
    rnd = random.Random(seed)
    c = [start]
    for _ in range(n - 1):
        c.append(c[-1] * math.exp(rnd.gauss(0, sd)))
    return c


def test_clean_series_sorts_dedups_and_drops_bad():
    ds, cs = ES.clean_series([["2020-01-03", 3], ["2020-01-01", "1"], ["2020-01-02", None],
                              ["2020-01-02", 0], ["2020-01-03", 4], ["2020-01-04", float("nan")]])
    assert ds == ["2020-01-01", "2020-01-03"] and cs == [1.0, 4.0]


def test_rolling_sigma_matches_naive():
    c = _walk(800)

    def naive(end, w):
        xs = c[max(0, end - w):end]
        r = [math.log(b / a) for a, b in zip(xs, xs[1:])]
        m = sum(r) / len(r)
        return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    R = ES._Roll(c)
    for end in (25, 250, 251, 799):
        assert abs(R.sigma(end, 250) - naive(end, 250)) < 1e-12
        assert abs(ES._sigma(c, end, 250) - naive(end, 250)) < 1e-12
    assert R.sigma(10, 250) is None                      # меньше 20 доходностей — честно None


def test_episode_move_base_is_close_before_trigger():
    ds = _dates(400)
    c = _walk(400)
    t = ds[300]
    m = ES.episode_move(ds, c, t)
    assert m["база_дата"] == ds[299] and m["база"] == c[299]
    assert abs(m["ходы"][5]["ход"] - math.log(c[304] / c[299])) < 1e-12
    assert m["ходы"][5]["дата"] == ds[304]
    assert ES.episode_move(ds, c, ds[0]) is None          # до начала истории — нет базы
    m2 = ES.episode_move(ds, c, ds[380])
    assert m2["ходы"][60] is None                         # горизонт за краем истории — честно None


def test_shock_events_finds_jump_without_lookahead_and_without_overlap():
    c = _walk(1200, sd=0.01, seed=3)
    for i in range(600, len(c)):                          # скачок +15% на баре 600
        c[i] *= 1.15
    ev = ES.shock_events(c)
    hits = [x for x in ev if 600 <= x["e"] <= 604]
    assert hits and hits[0]["ход"] > 0 and hits[0]["z"] >= ES.SHOCK_K
    es = [x["e"] for x in ev]
    assert all(b - a >= ES.GAP for a, b in zip(es, es[1:]))
    # σ события считается только по прошлому: будущий скачок не меняет более ранние события
    c2 = list(c)
    for i in range(1000, len(c2)):
        c2[i] *= 1.3
    early = [x for x in ES.shock_events(c2) if x["e"] < 990]
    assert [x["e"] for x in early] == [x["e"] for x in ev if x["e"] < 990]


def _pair(follow, n=2600, seed=5, sign=1, lag=3):
    """src — блуждание с редкими скачками; dst — своё блуждание, + (если follow) догоняет скачок src
    через lag дней в сторону sign."""
    rnd = random.Random(seed)
    ds = _dates(n)
    a = [100.0]
    jumps = set()
    for i in range(1, n):
        r = rnd.gauss(0, 0.01)
        if i > 300 and i % 61 == 0:
            r += 0.08 * (1 if rnd.random() < 0.5 else -1)
            jumps.add(i)
        a.append(a[-1] * math.exp(r))
    b = [50.0]
    pend = {}
    for i in range(1, n):
        r = rnd.gauss(0, 0.01)
        if follow and (i - lag) in jumps:
            ra = math.log(a[i - lag] / a[i - lag - 1])
            pend[i] = sign * (0.06 if ra > 0 else -0.06)
        r += pend.pop(i, 0.0)
        b.append(b[-1] * math.exp(r))
    return (ds, a), (ds, b)


def test_follow_stats_confirms_real_lagged_follow():
    src, dst = _pair(True)
    r = ES.follow_stats(src, dst, +1, horizon=5)
    assert r["событий"] >= ES.MIN_EVENTS and r["лет"] >= ES.MIN_YEARS
    assert r["доля"] > r["база"] and r["p"] <= 0.05 and r["вердикт"] == "подтверждена"
    # тот же ряд, но механизм с противоположным знаком — история против
    assert ES.follow_stats(src, dst, -1, horizon=5)["вердикт"] == "история против"


def test_follow_stats_independent_series_is_not_confirmed():
    src, _ = _pair(False, seed=7)
    _, dst = _pair(False, seed=11)
    r = ES.follow_stats(src, dst, +1, horizon=5)
    assert r["вердикт"] in ("история против", "намёк")


def test_follow_stats_direction_filter_and_small_data():
    src, dst = _pair(True)
    up = ES.follow_stats(src, dst, +1, horizon=5, src_dir=1)
    both = ES.follow_stats(src, dst, +1, horizon=5)
    assert up["шоки"] == "вверх" and 0 < up["событий"] < both["событий"]
    short = ES.follow_stats((src[0][:300], src[1][:300]), (dst[0][:300], dst[1][:300]), +1)
    assert short["вердикт"] == "мало данных"


def test_corr_sign_and_horizon_bucket():
    src, dst = _pair(True)
    ds, a = src
    neg = (ds, [1.0 / x for x in a])
    assert ES.corr_sign(src, src)[0] == 1 and ES.corr_sign(src, neg)[0] == -1
    assert [ES.horizon_bucket(x) for x in (0, 7, 8, 30, 31, None)] == [5, 5, 20, 20, 60, 5]


def _garch_pair(seed, n=2600):
    """Общий режим качки (GARCH(1,1)), знаки двух рядов независимы — направленной связи НЕТ."""
    rnd = random.Random(seed)
    ds = _dates(n)
    h, e_prev = 1e-4, 0.0
    a, b = [100.0], [100.0]
    for _ in range(1, n):
        h = 1e-6 + 0.09 * e_prev ** 2 + 0.90 * h
        s = math.sqrt(h)
        ea, eb = rnd.gauss(0, s), rnd.gauss(0, s)
        e_prev = ea
        a.append(a[-1] * math.exp(ea))
        b.append(b[-1] * math.exp(eb))
    return (ds, a), (ds, b)


def test_conditional_test_is_not_fooled_by_shared_volatility():
    # ревью 03.11: безусловная база давала p≤0.05 в ~15% таких тестов; условный знаковый — на уровне номинала
    pv, n = 0, 0
    for seed in range(30):
        src, dst = _garch_pair(seed)
        for sign in (1, -1):
            for d in (1, -1):
                r = ES.follow_stats(src, dst, sign, horizon=5, src_dir=d)
                if r.get("p") is not None:
                    n += 1
                    pv += r["p"] <= 0.05
    assert n >= 60 and pv / n <= 0.10


def test_episode_window_is_excluded_from_its_own_score():
    src, dst = _pair(True)
    full = ES.follow_stats(src, dst, +1, horizon=5)
    first = full["события"][0]["дата"]
    ex = ES.follow_stats(src, dst, +1, horizon=5, exclude=[first])
    assert ex["исключено_эпизодов"] >= 1 and ex["событий"] == full["событий"] - ex["исключено_эпизодов"]
    assert first not in [x["дата"] for x in ex["события"]]
