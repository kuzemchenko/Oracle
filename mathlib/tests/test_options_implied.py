# -*- coding: utf-8 -*-
"""Q-в-цене (09.11): что опционы закладывают к сроку идеи — расчёт кодом."""
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import options_implied as OI   # noqa: E402


def _row(exp, k, iv, typ="call", oi=100, bid=1.0, ask=1.1):
    return {"id": f"X-{exp}-2026-11-06", "attributes": {"exp_date": exp, "strike": k, "volatility": iv, "type": typ,
                                                      "open_interest": oi, "bid": bid, "ask": ask,
                                                      "previous_date": "2026-11-06"}}


CHAIN = [_row("2026-11-27", 90, 0.50), _row("2026-11-27", 100, 0.40), _row("2026-11-27", 110, 0.45),
         _row("2027-01-15", 90, 0.42), _row("2027-01-15", 100, 0.35), _row("2027-01-15", 110, 0.38),
         _row("2027-01-15", 120, 0.40, bid=0.0, ask=2.0)]          # широкий спред — отброшен


def test_parse_drops_unreliable_quotes():
    chain, dropped = OI.parse_chain(CHAIN + [_row("2027-01-15", 100, None)])
    assert len(chain) == 6 and dropped["широкий_спред"] == 1 and dropped["нет_iv"] == 1


def test_expiry_not_before_idea_date():
    chain, _ = OI.parse_chain(CHAIN)
    assert OI.pick_expiry(chain, "2026-12-10") == ("2027-01-15", None)
    exp, note = OI.pick_expiry(chain, "2027-03-01")
    assert exp == "2027-01-15" and "последняя" in note


def test_iv_interpolation_and_move():
    chain, _ = OI.parse_chain(CHAIN)
    assert abs(OI.iv_at(chain, "2027-01-15", 105) - 0.365) < 1e-9
    mv = OI.implied_move(100, 0.35, 73)
    assert abs(mv["доля"] - 0.35 * math.sqrt(73 / 365)) < 1e-4 and mv["диапазон_68"][0] < 100 < mv["диапазон_68"][1]


def test_risk_neutral_probability_sane():
    assert abs(OI.prob_beyond(100, 100, 0.3, 365) - 0.4404) < 1e-3           # N(-σ√t/2) при K=S
    up = OI.prob_beyond(100, 130, 0.3, 90)
    assert 0 < up < 0.1 and abs(up + OI.prob_beyond(100, 130, 0.3, 90, "below") - 1) < 1e-9
    assert OI.prob_beyond(100, 130, 0.3, 0) is None                           # П8: нет срока — нет числа


def test_market_expectation_full_and_refusals():
    r = OI.market_expectation(CHAIN, 100.0, "2026-11-09", "2026-12-20", threshold=110, direction="above",
                              spot_date="2026-11-06")
    assert r["ok"] and r["экспирация"] == "2027-01-15" and r["дней"] == 67
    assert 0.1 < r["вероятность_рн"] < 0.4 and r["нужный_ход"] == 0.1
    assert "двух случаях из трёх" in OI.words(r) and "не точная" in OI.words(r)
    assert not OI.market_expectation([], 100, "2026-11-09", "2026-12-20")["ok"]
    assert not OI.market_expectation(CHAIN, None, "2026-11-09", "2026-12-20")["ok"]
    thin = OI.market_expectation([_row("2027-01-15", 100, 0.3, oi=1)], 100, "2026-11-09", "2026-12-20")
    assert any("тонкие" in o for o in thin["оговорки"])
