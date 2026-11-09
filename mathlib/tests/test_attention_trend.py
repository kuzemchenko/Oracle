# -*- coding: utf-8 -*-
"""Q-тренды (09.11): фазы растущего внимания по ежедневному ряду."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import attention_trend as AT   # noqa: E402

BASE = [100] * 56


def test_quiet_and_refusals():
    assert AT.trend(BASE + [105] * 7)["фаза"] == "тишина"
    assert not AT.trend([100] * 20)["ok"]
    assert "тихая" in AT.trend([5] * 63)["причина"]


def test_early_rise_vs_spike():
    early = AT.trend(BASE + [110, 130, 150, 160, 170, 180, 190])
    assert early["фаза"] == "ранний рост" and early["дней_выше_нормы"] >= 3
    one_day = AT.trend(BASE + [100, 100, 100, 100, 100, 100, 900])        # один всплеск ≠ тренд
    assert one_day["фаза"] in ("тишина", "ранний рост") and one_day["дней_выше_нормы"] == 1


def test_surge_peak_and_fade():
    assert AT.trend(BASE + [300, 350, 400, 420, 450, 480, 500])["фаза"] == "разгон"
    assert AT.trend(BASE + [700, 800, 900, 1000, 1100, 1200, 1300])["фаза"] == "пик"
    fade = AT.trend([100] * 49 + [900, 1200, 800, 400, 200, 150, 130] + [120, 110, 110, 105, 100, 100, 100])
    assert fade["фаза"] == "угасание"
    assert "толпа уходит" in AT.words("X", fade) and "не измерено" in AT.words("X", {"ok": False, "причина": "мало"})
