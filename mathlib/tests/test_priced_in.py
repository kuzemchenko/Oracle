# -*- coding: utf-8 -*-
"""Тесты «что уже в цене» (mathlib/priced_in.py, подписан 01.11).

Закрепляют: парсер амплитуд из текстов библиотеки (проценты/диапазоны/кратные/±),
вилку доли отыгранности, вердикты (РАНО/В ПУТИ/В ЦЕНЕ/ПЕРЕЛЁТ/ПРОТИВ), честные
ОТКАЗы (П8) и покрытие парсером БОЕВОЙ библиотеки механизмов."""
import pathlib
import sys

import yaml

from mathlib import priced_in as P

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_parse_percent_range_and_multiples():
    r = P.parse_amplitude("1-й ход: нефть +30–130% за 2–10 недель; газ TTF ×2–3 за недели")
    assert r[0] == {"низ": 30.0, "верх": 130.0, "вид": "проценты"}
    assert {"низ": 100.0, "верх": 200.0, "вид": "кратно"} in r
    # «за 2–10 недель» без % — не амплитуда
    assert not any(x["низ"] == 2.0 and x["верх"] == 10.0 for x in r)


def test_parse_negative_twosided_single():
    assert P.parse_amplitude("индекс −3…−8% за дни")[0] == {
        "низ": -8.0, "верх": -3.0, "вид": "проценты"}
    assert P.parse_amplitude("±8–31% за день")[0]["вид"] == "проценты±"
    assert P.parse_amplitude("розничное топливо +10%")[0] == {
        "низ": 10.0, "верх": 10.0, "вид": "проценты"}
    assert P.parse_amplitude("кратно, без цифр") == []


def test_fraction_verdict_ladder():
    assert P.played_fraction(5, "+30–130%")["вердикт"] == P.РАНО
    assert P.played_fraction(50, "×2–3")["вердикт"] == P.В_ПУТИ         # 25–50%
    out = P.played_fraction(40, "+30–130%")
    assert out["вердикт"] == P.В_ЦЕНЕ
    assert "от 31% до всего прецедентного хода" in out["строка"]   # >100% не пугаем словами
    assert "впереди" in P.played_fraction(5, "+30–130%")["строка"]
    assert P.played_fraction(-12, "−3…−8%")["вердикт"] == P.ПЕРЕЛЁТ     # за дальним краем
    assert P.played_fraction(-5, "−3…−8%")["вердикт"] == P.В_ЦЕНЕ       # внутри диапазона


def test_against_mechanism_and_twosided():
    out = P.played_fraction(-5, "+30–130%")
    assert out["вердикт"] == P.ПРОТИВ and "ПРОТИВ механизма" in out["строка"]
    # двусторонняя амплитуда (±) знак не проверяет
    assert P.played_fraction(-10, "±8–31% за день")["вердикт"] == P.В_ЦЕНЕ


def test_honest_refusals():
    assert "ОТКАЗ" in P.played_fraction(10, "кратно растёт")
    assert "ОТКАЗ" in P.played_fraction("не число", "+10–20%")
    assert "ОТКАЗ" in P.played_fraction(10, "+10–20%", range_index=5)


def test_battle_library_parse_coverage():
    ms = yaml.safe_load((ROOT / "knowledge" / "mechanisms.yaml")
                        .read_text(encoding="utf-8"))["механизмы"]
    parsed = [m["id"] for m in ms if P.parse_amplitude(m.get("amplitude"))]
    assert len(parsed) >= 45, f"парсер покрывает {len(parsed)}/{len(ms)} механизмов"
