# -*- coding: utf-8 -*-
import math

import pytest

from mathlib import scenario_stats as SS


def _ep(i, date, tag, h20, sym="BNO"):
    return {"id": i, "trigger_date": date, "links": [
        {"role_tag": "other", "today_symbol": "X", "prices": {"есть": True, "ход_20": 0.5}},
        {"role_tag": tag, "today_symbol": sym, "prices": {"есть": True, "ход_20": h20, "тикер": "BZ=F"}}]}


def test_episode_moves_by_role_and_simple_percent():
    eps = [_ep("a", "2019-06-13", "crude_oil_brent", math.log(1.109)), _ep("b", "2023-10-09", "crude_oil_brent", math.log(1.004)),
           {"id": "c", "trigger_date": "2024", "links": [{"role_tag": "crude_oil_brent", "prices": {"есть": False}}]}]
    m = SS.episode_moves(eps, role_tag="crude_oil_brent")
    assert [x["эпизод"] for x in m] == ["a", "b"] and m[0]["ход_проц"] == pytest.approx(10.9)
    assert SS.episode_moves(eps, symbol="BNO.US")[1]["ход_проц"] == pytest.approx(0.4)
    assert len(SS.episode_moves(eps, role_tag=["crude_oil", "crude_oil_brent"])) == 2      # синонимы ролей
    both = [{"id": "d", "trigger_date": "2019", "links": [
        {"role_tag": "tanker", "today_symbol": "FRO", "prices": {"есть": True, "ход_20": math.log(1.12)}},
        {"role_tag": "tanker", "today_symbol": "DHT", "prices": {"есть": True, "ход_20": math.log(1.14)}}]}]
    assert SS.episode_moves(both, role_tag=["tanker"], symbol="DHT.US")[0]["ход_проц"] == pytest.approx(14.0)
    with pytest.raises(ValueError):
        SS.episode_moves(eps, role_tag="x", horizon=7)


def test_summary_marks_few_cases_and_direction():
    s = SS.summary([{"ход_проц": 10.9}, {"ход_проц": 0.4}], "up")
    assert s["n"] == 2 and s["мало_случаев"] and s["медиана_проц"] == pytest.approx(5.65) and s["в_сторону"] == 2
    s3 = SS.summary([{"ход_проц": -5}, {"ход_проц": 3}, {"ход_проц": -8}], "down")
    assert not s3["мало_случаев"] and s3["в_сторону"] == 2 and s3["медиана_проц"] == -5
    assert SS.summary([], "up")["n"] == 0


def test_passed_and_levels():
    assert SS.passed(100, 105, 10) == 0.5
    assert SS.passed(100, 90, -20) == 0.5            # падение: пройдено половина хода вниз
    assert SS.passed(0, 1, 1) is None and SS.passed(100, 105, 0) is None
    lv = SS.levels(103, 100, 10, "up")
    assert lv == {"цель": 110.0, "стоп": pytest.approx(97.85)}
    down = SS.levels(95, 100, 20, "down")
    assert down["цель"] == 80.0 and down["стоп"] == pytest.approx(104.5)
    gone = SS.levels(112, 100, 10, "up")
    assert "уже пройден" in gone["причина"] and "стоп" not in gone
