# -*- coding: utf-8 -*-
"""Q-в-цене (09.11): загрузка опционов EODHD (только последний день) и строка в оговорках карточки."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops import options_view as OV      # noqa: E402
from ops import evidence_check as EC    # noqa: E402


def _r(c, exp, k, iv, day):
    return {"id": f"{c}-{day}", "attributes": {"contract": c, "exp_date": exp, "strike": k, "volatility": iv,
                                               "type": "call", "open_interest": 50, "bid": 1, "ask": 1.1}}


def test_fetch_keeps_latest_day_and_sends_trade_filter():
    seen = []

    def get(url, params):
        seen.append(params)
        return 200, {"data": [_r("A", "2027-01-15", 100, 0.3, "2026-11-05"), _r("A", "2027-01-15", 100, 0.31, "2026-11-06"),
                              _r("B", "2027-01-15", 110, 0.33, "2026-11-05")]}
    rows, err = OV.fetch("FSLR.US", "2026-11-09", "2027-03-01", get=get, trade_from="2026-11-02")
    assert err is None and seen[0]["filter[tradetime_from]"] == "2026-11-02"
    assert seen[0]["filter[underlying_symbol]"] == "FSLR"
    assert [r["id"] for r in rows] == ["A-2026-11-06"]        # только последний торговый день


def test_view_with_spot_and_words():
    def get(url, params):
        return 200, {"data": [_r("A", "2027-01-15", 100, 0.35, "2026-11-06"), _r("B", "2027-01-15", 120, 0.38, "2026-11-06")]}
    res = OV.view("FSLR", "2026-12-20", threshold=110, spot=100.0, spot_date="2026-11-06", today="2026-11-09", get=get)
    assert res["ok"] and res["экспирация"] == "2027-01-15" and "опционы" in res["источник"]
    assert res["словами"].startswith("Опционы до 2027-01-15")
    bad = OV.view("FSLR", "2026-12-20", spot=100.0, today="2026-11-09", get=lambda u, p: (403, "нет доступа"))
    assert not bad["ok"] and "403" in bad["причина"]


def test_evidence_label_shows_market_vs_ours():
    card = {"spec9": {"probability": 0.45}, "options_view": {"ok": True, "экспирация": "2027-01-15",
                                                             "ход_1сигма": {"доля": 0.22}, "вероятность_рн": 0.23}}
    labs = []
    EC.check_options(card, [], labs)
    assert "±22%" in labs[0] and "23%" in labs[0] and "наша оценка — 45%" in labs[0]
    labs = []
    EC.check_options({}, [], labs)
    assert "не посчитано" in labs[0]


def test_week_odds_annotates_options_before_seal_and_renders():
    from orchestrator import week_odds as WO
    spec = {"asset": "FSLR.US", "resolve_by": "2026-12-20T21:00:00", "threshold": 230.0, "direction": "above"}
    WO.annotate_options(spec, lambda s: {"ok": True, "вероятность_рн": 0.23, "экспирация": "2027-01-15",
                                         "ход_1сигма": {"доля": 0.22}})
    assert spec["опционы"]["вероятность_рн"] == 0.23
    bad = WO.annotate_options(dict(spec), lambda s: (_ for _ in ()).throw(RuntimeError("сеть")))
    assert "нет" in bad["опционы"]
    assert "опционы" not in WO.annotate_options({"asset": "X"}, None)


def test_week_odds_render_shows_market_line():
    from ops import bot_reports as BR
    q = {"asset": "FSLR.US", "вопрос": "First Solar (FSLR.US) закроется выше $230.00 к 20 декабря",
         "probability": 0.45, "опционы": {"вероятность_рн": 0.23}}
    text, _viol = BR.format_week_odds({"вопросы": [q], "запечатано": 1, "принято": 1})
    assert "Рынок по ценам опционов: около 23%" in text and "около 45%" in text
