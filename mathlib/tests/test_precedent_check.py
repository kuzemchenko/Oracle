# -*- coding: utf-8 -*-
"""V6 Q-прецеденты (09.11): заявленный ход прецедента против настоящих закрытий."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import precedent_check as PC   # noqa: E402

D = ["2022-02-22", "2022-02-23", "2022-02-24", "2022-03-01", "2022-03-08", "2022-03-15"]
C = [96.0, 97.0, 105.0, 110.0, 128.0, 99.0]


def test_level_and_peak():
    lv = PC.actual_move(D, C, "2022-02-23", "2022-03-15", "level")
    assert lv["начало"] == ["2022-02-23", 97.0] and abs(lv["ход"] - (99 / 97 - 1)) < 1e-4
    pk = PC.actual_move(D, C, "2022-02-23", "2022-03-15", "peak", 1)
    assert pk["экстремум"] == ["2022-03-08", 128.0] and abs(pk["ход"] - (128 / 97 - 1)) < 1e-4


def test_verdicts():
    assert PC.verdict(30, 0.32) == ("подтверждён", 1.067)
    assert PC.verdict(30, 0.05)[0] == "по знаку"
    assert PC.verdict(30, -0.1)[0] == "не подтверждён"
    assert PC.verdict(None, 0.1)[0].startswith("ход посчитан")
    assert PC.verdict(30, None)[0] == "нет данных"


def test_quote_must_be_in_text_and_missing_data():
    obs = "Brent >$105 в день 1, ~$129 к началу марта"
    ok = PC.check({"asset": "BZ=F", "start": "2022-02-23", "end": "2022-03-15", "claimed_pct": 32,
                   "quote": "~$129 к началу марта", "kind": "peak"}, obs, (D, C))
    assert ok["вердикт"] == "подтверждён" and ok["факт_проц"] == 32.0
    bad = PC.check({"asset": "BZ=F", "start": "2022-02-23", "end": "2022-03-15", "claimed_pct": 50,
                    "quote": "+50% за неделю"}, obs, (D, C))
    assert bad["вердикт"] == "разметка отвергнута"
    assert PC.check({"asset": None, "note": "макростатистика"}, obs, None)["вердикт"] == "нет данных"
    assert PC.check({"asset": "X", "start": "2022-02-23", "end": "2022-03-15", "claimed_pct": None}, obs, None)[
        "вердикт"] == "нет данных"


def test_dead_series_and_fx_slack():
    d = ["2018-04-04", "2018-04-05", "2018-04-06"]
    dead = PC.check({"asset": "ALI=F", "start": "2018-04-04", "end": "2018-04-06", "claimed_pct": 10, "quote": "+10%"},
                    "алюминий +10%", (d, [2300.0, 2300.0, 2300.0]))
    assert dead["вердикт"] == "нет данных" and "мёртвый" in dead["причина"]
    fx = (["2022-12-19", "2022-12-20", "2022-12-21"], [0.00732, 0.0073, 0.00758])
    ann = {"asset": "JPYUSD=X", "start": "2022-12-19", "end": "2022-12-20", "claimed_pct": 4, "quote": "иена +4%"}
    assert PC.check(ann, "иена +4%", fx)["вердикт"] == "не подтверждён"
    assert PC.check({**ann, "slack_days": 1}, "иена +4%", fx)["вердикт"] == "подтверждён"
