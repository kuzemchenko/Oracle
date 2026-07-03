# -*- coding: utf-8 -*-
"""Тесты датчика перегретости/неочевидности (mathlib/attention.py, П1)."""
from mathlib import attention as A


def test_cold_theme_low_score_phase_rano():
    # Тема вне радара: интерес низко в СВОЁМ диапазоне, спад к концу → свежо, фаза РАНО.
    interest = [80, 70, 60, 40, 30, 20, 15, 12, 10, 8]
    r = A.attention_score(interest)
    assert r["score"] is not None
    assert r["свежесть"] > 0.5           # неочевидно
    assert r["фаза"] == "РАНО"
    assert 0.0 <= r["score"] <= 1.0


def test_hot_peak_high_score_late():
    # Внимание на историческом пике окна и плато → перегрето, фаза ПОЗДНО.
    interest = [5, 8, 10, 20, 40, 70, 95, 96, 97, 96]
    r = A.attention_score(interest)
    assert r["score"] > 0.6
    assert r["свежесть"] < 0.4
    assert r["уровень"] >= A.LEVEL_HOT
    assert r["фаза"] in ("ПОЗДНО", "ВОВРЕМЯ")   # высоко; плато→ПОЗДНО, ещё ускоряется→ВОВРЕМЯ


def test_blowoff_rollover_is_trap():
    # Взлетело на недавний пик и перекатывается вниз (пик пройден) → ЛОВУШКА.
    interest = [10, 15, 30, 55, 80, 100, 95, 78, 60, 45]
    r = A.attention_score(interest)
    assert r["фаза"] == "ЛОВУШКА"
    assert r["наклон"] < 0                # честно остывает


def test_rising_from_low_is_early_or_timely():
    # Разгорается из низкого — импульс вверх, но уровень ещё не на пике.
    interest = [10, 9, 11, 10, 12, 14, 18, 22, 28, 33]
    r = A.attention_score(interest)
    assert r["наклон"] > 0
    assert r["фаза"] in ("РАНО", "ВОВРЕМЯ")


def test_insufficient_history_returns_none():
    r = A.attention_score([10, 20, 30])
    assert r["score"] is None
    assert r["фаза"] is None
    assert "мало истории" in r["провенанс"]


def test_flat_series_no_signal():
    # Плоский ряд: относительного сигнала внимания нет → честный None (П8), не выдумка.
    r = A.attention_score([50] * 10)
    assert r["score"] is None
    assert "вариаци" in r["провенанс"].lower() or "плоск" in r["провенанс"].lower()


def test_empty_input_none():
    r = A.attention_score([])
    assert r["score"] is None
    assert r["n"] == 0


def test_partial_tail_trimmed():
    # Последняя корзина Trends неполна (is_partial) и занижена — её нельзя принимать за «остывание».
    interest = [20, 30, 45, 60, 75, 88, 95, 97, 98, 40]
    flags = [False] * 9 + [True]        # последний — партиал (заниженный)
    r = A.attention_score(interest, is_partial=flags)
    assert "срезан незавершённый хвост" in r["заметка"]
    assert r["n"] == 9                  # партиал-хвост отброшен
    # без ложного партиала последнее полное значение высокое → перегрето, не ЛОВУШКА
    assert r["уровень"] >= A.LEVEL_HOT


def test_score_bounds_and_freshness_complement():
    interest = [12, 18, 25, 40, 55, 70, 60, 50, 45, 42]
    r = A.attention_score(interest)
    assert 0.0 <= r["score"] <= 1.0
    assert abs(r["score"] + r["свежесть"] - 1.0) < 1e-9


def test_attention_map_groups_by_keyword():
    rows = [("brent oil", "2026-05-01", 10), ("brent oil", "2026-05-08", 20),
            ("brent oil", "2026-05-15", 30), ("brent oil", "2026-05-22", 45),
            ("brent oil", "2026-05-29", 60), ("brent oil", "2026-06-05", 80),
            ("brent oil", "2026-06-12", 95), ("brent oil", "2026-06-19", 97),
            ("uranium", "2026-06-01", 40), ("uranium", "2026-06-08", 42)]
    m = A.attention_map(rows)
    assert set(m.keys()) == {"brent oil", "uranium"}
    assert m["brent oil"]["score"] is not None      # 8 точек — хватает
    assert m["uranium"]["score"] is None            # 2 точки — честный None


def test_from_rows_sorts_by_date():
    # Строки вперемешку по дате — датчик обязан отсортировать перед расчётом импульса.
    rows = [("2026-06-19", 95), ("2026-05-01", 10), ("2026-06-05", 80),
            ("2026-05-15", 30), ("2026-05-29", 60), ("2026-05-08", 20),
            ("2026-06-12", 92), ("2026-05-22", 45)]
    r = A.attention_from_rows(rows)
    assert r["score"] is not None
    assert r["наклон"] > 0              # по возрастанию даты ряд растёт
