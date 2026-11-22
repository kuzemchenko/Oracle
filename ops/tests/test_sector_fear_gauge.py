# -*- coding: utf-8 -*-
"""Q-цена-страха (22.11): датчик «цена защиты» в карте секторов. Герметично: цены и опционы подставляются,
сети, базы и боевых снимков нет."""
import datetime
import json
import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import sector_view as SV        # noqa: E402
from mathlib import fear_gauge as FG    # noqa: E402

TODAY = datetime.date(2026, 11, 22)
RULES = dict(SV.load_rules(), карта=["XLK.US", "XLE.US"])


def _closes(n=900, daily_sd=0.01, last=TODAY - datetime.timedelta(days=2), seed=11):
    rnd = random.Random(seed)
    v = [100.0]
    for _ in range(n - 1):
        v.append(v[-1] * math.exp(rnd.gauss(0.0, daily_sd)))
    return [((last - datetime.timedelta(days=n - 1 - i)).isoformat(), x) for i, x in enumerate(v)]


LONG = _closes()
USUAL = FG.measures(LONG)["обычные"]


def _fetch(iv_mult=2.0, implied=None, long=LONG, seen=None):
    def closes(sym, today):
        return _closes(270, seed=3)

    def closes_long(sym, today):
        if seen is not None:
            seen.append(("цены", sym))
        return long

    def impl(sym, today, spot, spot_date=None):
        if seen is not None:
            seen.append(("опционы", sym, spot, spot_date))
        return {"iv": round(USUAL * iv_mult, 4), "экспирация": "2026-12-24", "дней": 32, "котировки_на": "2026-11-21"}

    return {"closes": closes, "general": lambda sym: None, "closes_long": closes_long, "implied": implied or impl}


# ── расчёт датчика ──
def test_gauge_measured_from_map_prices_and_options():
    seen = []
    g = SV.fear_gauge(TODAY, RULES, _fetch(2.0, seen=seen))
    assert g["вердикт"] == "дорогая" and g["к_обычным"] == 2.0 and g["не_измерено"] is None
    assert g["рынок"] == "SPY.US" and g["дата"] == "2026-11-20" and g["дней_обычных"] == 756
    assert g["опционы"] == {"экспирация": "2026-12-24", "дней": 32, "котировки_на": "2026-11-21"}
    # цены — широкого рынка из листа комитета; опционам передана последняя цена этого же ряда и её дата
    assert seen == [("цены", "SPY.US"), ("опционы", "SPY.US", LONG[-1][1], "2026-11-20")]
    assert SV.fear_gauge(TODAY, RULES, _fetch(0.9))["вердикт"] == "дешёвая"
    assert SV.fear_gauge(TODAY, RULES, _fetch(1.2))["вердикт"] == "обычная"


def test_gauge_not_measured_keeps_realized_numbers():
    def boom(*a, **k):
        raise OSError("сеть упала")

    g = SV.fear_gauge(TODAY, RULES, _fetch(implied=boom))                     # сбой сети
    assert g["вердикт"] is None and "опционы не получены (OSError)" == g["не_измерено"]
    assert g["обычные"] == USUAL and g["за_месяц"] is not None and g["закладывают"] is None and g["опционы"] is None
    g = SV.fear_gauge(TODAY, RULES, _fetch(implied=lambda *a, **k: {"причина": "опционы не получены (EODHD ответил 401)"}))
    assert g["вердикт"] is None and g["не_измерено"] == "опционы не получены (EODHD ответил 401)"
    g = SV.fear_gauge(TODAY, RULES, _fetch(implied=lambda *a, **k: None))
    assert g["вердикт"] is None and g["не_измерено"] == "нет данных об опционах"
    f = _fetch()
    del f["implied"]                                                          # источника опционов нет вовсе
    assert SV.fear_gauge(TODAY, RULES, f)["не_измерено"] == "нет данных об опционах"


def test_gauge_without_prices_or_with_stale_prices_is_not_measured():
    called = []
    impl = lambda *a, **k: called.append(1) or {"iv": 0.3}                    # noqa: E731
    g = SV.fear_gauge(TODAY, RULES, _fetch(implied=impl, long=None))
    assert g["вердикт"] is None and g["не_измерено"] == "нет цен широкого рынка" and g["обычные"] is None
    old = _closes(last=TODAY - datetime.timedelta(days=12))
    g = SV.fear_gauge(TODAY, RULES, _fetch(implied=impl, long=old))
    assert g["вердикт"] is None and "устарела" in g["не_измерено"] and g["обычные"] is not None
    assert called == []                                                       # к поставщику опционов не ходили
    assert SV.fear_gauge(TODAY, RULES, {"closes": lambda s, t: None})["не_измерено"] == "нет цен широкого рынка"
    short = _closes(300)                                                      # цены и опционы есть, истории мало
    g = SV.fear_gauge(TODAY, RULES, _fetch(long=short))
    assert g["вердикт"] is None and "мало истории" in g["не_измерено"] and g["закладывают"] is not None


# ── снимок ──
def test_tick_writes_gauge_under_own_key_and_keeps_old_keys(tmp_path):
    f = _fetch(2.0)
    m = SV.tick(TODAY, tmp_path, RULES, f)
    plain = SV.sector_map(TODAY, RULES, f)
    for name in ("2026-11-22.json", "latest.json"):
        d = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        assert set(d) == {"дата", "секторы", "цена_защиты"}
        assert d["дата"] == plain["дата"] and d["секторы"] == json.loads(json.dumps(plain["секторы"]))   # прежний формат
        assert d["цена_защиты"]["вердикт"] == "дорогая" and d["цена_защиты"]["закладывают"] == round(USUAL * 2, 4)
        assert d["цена_защиты"] == json.loads(json.dumps(m["цена_защиты"]))
    # история копится в датированных файлах
    SV.tick(TODAY + datetime.timedelta(days=1), tmp_path, RULES, _fetch(0.9, long=_closes(last=TODAY - datetime.timedelta(days=1))))
    assert json.loads((tmp_path / "2026-11-22.json").read_text(encoding="utf-8"))["цена_защиты"]["вердикт"] == "дорогая"
    assert json.loads((tmp_path / "2026-11-23.json").read_text(encoding="utf-8"))["цена_защиты"]["вердикт"] == "дешёвая"
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["цена_защиты"]["вердикт"] == "дешёвая"


def test_tick_survives_gauge_failure_and_old_style_sources(tmp_path, monkeypatch):
    old_style = {"closes": lambda sym, today: _closes(270, seed=3)}              # источники без датчика (прежние тесты)
    m = SV.tick(TODAY, tmp_path, RULES, old_style)
    assert len(m["секторы"]) == 2 and m["цена_защиты"]["вердикт"] is None
    monkeypatch.setattr(SV, "fear_gauge", lambda *a, **k: 1 / 0)
    m = SV.tick(TODAY, tmp_path, RULES, old_style)
    d = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert len(d["секторы"]) == 2 and d["цена_защиты"] == {"вердикт": None, "не_измерено": "сбой расчёта (ZeroDivisionError)"}


def test_default_sources_under_tests_touch_no_network(tmp_path):
    """Предохранитель conftest подменяет источники карты — датчик при этом «не измерен», а не идёт в сеть."""
    m = SV.tick(TODAY, tmp_path, RULES)
    assert m["цена_защиты"]["вердикт"] is None and m["цена_защиты"]["не_измерено"] == "нет цен широкого рынка"


# ── слова: сводка агента и карта ──
ROWS = [{"имя": "чипы", "фонд": "SMH.US", "статус": "корректируется", "почему": ["ниже своей средней"],
         "меры": {"за_3_месяца": -0.27, "рынок_за_3_месяца": -0.10}},
        {"имя": "здравоохранение", "фонд": "XLV.US", "статус": "попутный", "почему": ["выше своей средней"],
         "меры": {"за_3_месяца": 0.01, "рынок_за_3_месяца": -0.10}}]
G_22NOV = {"рынок": "SPY.US", "закладывают": 0.36, "за_месяц": 0.26, "обычные": 0.16, "вердикт": "дорогая",
           "почему": [], "опционы": None, "не_измерено": None}
LINE_22NOV = ("Цена защиты: дорогая — опционы на S&P 500 (фонд SPY) закладывают 36% годовых колебаний при фактических "
              "26% за месяц и обычных 16%; защиту от падения (опцион «пут») и ставки на падение лучше покупать, когда "
              "она дешёвая, а не после всплеска страха.")


def _snap(tmp_path, gauge=None, with_key=True):
    m = {"дата": "2026-11-22", "секторы": ROWS}
    if with_key:
        m["цена_защиты"] = gauge
    (tmp_path / "latest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


def test_market_brief_adds_one_phrase_only_when_measured(tmp_path):
    _snap(tmp_path, with_key=False)
    base = SV.market_brief(TODAY, tmp_path)
    assert base and "Цена защиты" not in base
    _snap(tmp_path, G_22NOV)
    assert SV.market_brief(TODAY, tmp_path) == base + " " + LINE_22NOV
    for unmeasured in (None, {}, "мусор", dict(G_22NOV, вердикт=None, закладывают=None, не_измерено="нет данных об опционах"),
                       dict(G_22NOV, вердикт="непонятная")):
        _snap(tmp_path, unmeasured)
        assert SV.market_brief(TODAY, tmp_path) == base                       # нет измерения — фразы нет, текст прежний


def test_render_shows_same_line_at_the_bottom(tmp_path):
    plain = SV.render({"дата": "2026-11-22", "секторы": ROWS})
    assert "Цена защиты" not in plain                                         # снимок без датчика — карта прежняя
    txt = SV.render({"дата": "2026-11-22", "секторы": ROWS, "цена_защиты": G_22NOV})
    assert txt == plain + "\n\n" + LINE_22NOV
    g = dict(G_22NOV, вердикт=None, закладывают=None, не_измерено="опционы не получены (EODHD ответил 401)")
    last = SV.render({"дата": "2026-11-22", "секторы": ROWS, "цена_защиты": g}).splitlines()[-1]
    assert last == ("Цена защиты: не измерена — опционы не получены (EODHD ответил 401). Фактические годовые колебания "
                    "S&P 500 (фонд SPY): 26% за месяц, обычно 16%.")


def test_market_name_spells_out_the_fund():
    assert SV.market_name("SPY") == "S&P 500 (фонд SPY)" and SV.market_name("VTI.US") == "широкий рынок (фонд VTI)"
    g = dict(G_22NOV, рынок="VTI.US", опционы={"экспирация": "2026-12-24"})
    assert "опционы на широкий рынок (фонд VTI) закладывают 36% годовых колебаний (на срок до 2026-12-24)" in SV.fear_line(g)


# ── источники ──
def _opt(exp, strike, iv, typ="call", day="2026-11-20"):
    return {"id": f"SPY-{exp}-{typ}-{strike}-{day}",
            "attributes": {"contract": f"SPY{exp}{typ}{strike}", "exp_date": exp, "strike": strike, "volatility": iv,
                           "type": typ, "open_interest": 5000, "bid": 1.0, "ask": 1.1, "previous_date": day}}


def test_fetch_implied_takes_month_ahead_expiry_not_the_nearest(monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    asked = []

    def get(url, params):
        asked.append(params)
        return 200, {"data": [_opt("2026-12-24", 680, 0.31), _opt("2026-12-24", 700, 0.29),
                              _opt("2026-12-24", 680, 0.31, "put"), _opt("2026-12-31", 690, 0.28)]}

    r = SV.fetch_implied("SPY.US", TODAY, 690.0, "2026-11-20", get=get)
    assert r["iv"] == 0.30 and r["экспирация"] == "2026-12-24" and r["дней"] == 32 and r["котировки_на"] == "2026-11-20"
    p = asked[0]
    assert p["filter[underlying_symbol]"] == "SPY" and p["filter[exp_date_from]"] == "2026-12-22"      # +30 дней
    assert p["filter[exp_date_to]"] == "2027-01-12" and p["filter[tradetime_from]"] == "2026-11-16"
    assert "api_token" not in p


def test_fetch_implied_failures_are_reasons_not_numbers(monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    assert SV.fetch_implied("SPY.US", TODAY, 690.0, "2026-11-20", get=lambda u, p: (401, "Unauthenticated")) == {
        "причина": "опционы не получены (EODHD ответил 401)"}
    r = SV.fetch_implied("SPY.US", TODAY, 690.0, "2026-11-20", get=lambda u, p: (200, {"data": []}))
    assert "iv" not in r and "нет пригодных опционов" in r["причина"]
    thin = {"data": [dict(_opt("2026-12-24", 690, 0.3), attributes=dict(_opt("2026-12-24", 690, 0.3)["attributes"],
                                                                        open_interest=2))]}
    r = SV.fetch_implied("SPY.US", TODAY, 690.0, "2026-11-20", get=lambda u, p: (200, thin))
    assert "iv" not in r and "тонкие" in r["причина"]                           # тонкий рынок опционов — не измерение


def test_fetch_closes_depth_is_passed_only_when_asked(monkeypatch):
    seen = []
    monkeypatch.setattr(SV, "_db_closes", lambda sym, today, db_path=None, **kw: seen.append(kw) or _closes(30))
    monkeypatch.setattr(SV, "_api_closes", lambda sym, today, **kw: None)
    SV.fetch_closes("SPY.US", TODAY)
    SV.fetch_closes_long("SPY.US", TODAY)
    assert seen == [{}, {"days": SV.FEAR_HISTORY_DAYS}]                       # карта — прежний вызов; датчик — ~3 года
    assert SV.FEAR_HISTORY_DAYS >= 756 * 365 / 252 + 60                      # хватает на 756 торговых дней с запасом


def test_db_closes_reads_requested_depth(tmp_path):
    import sqlite3
    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("create table quotes (symbol text, date text, close real, adjusted_close real)")
    con.executemany("insert into quotes values ('SPY.US', ?, ?, ?)",
                    [((TODAY - datetime.timedelta(days=i)).isoformat(), 100.0, 100.0) for i in range(1500)])
    con.commit()
    con.close()
    assert len(SV._db_closes("SPY.US", TODAY, db)) == SV.HISTORY_DAYS + 1
    assert len(SV._db_closes("SPY.US", TODAY, db, days=SV.FEAR_HISTORY_DAYS)) == SV.FEAR_HISTORY_DAYS + 1
