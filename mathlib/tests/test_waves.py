# -*- coding: utf-8 -*-
"""Тесты числовой разметки волн Эллиотта (§4 «Волновик», §21).

Проверяем ДЕТЕРМИНИРОВАННУЮ часть: пивоты ZigZag, отношения Фибоначчи, три жёстких правила
импульса (валидный счёт и каждое нарушение по отдельности), измерение ABC и общую разметку.
"""
import sys
import pathlib

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import waves as W  # noqa: E402


def _build(pivot_prices, steps=10):
    """Кусочно-линейный ряд по заданным ценам пивотов (монотонные ноги → ZigZag берёт ровно их)."""
    series = [float(pivot_prices[0])]
    for a, b in zip(pivot_prices, pivot_prices[1:]):
        series.extend(np.linspace(a, b, steps + 1)[1:].tolist())
    return series


def _pivs(prices, kinds):
    return [{"index": i, "price": float(p), "kind": k, "confirmed": True}
            for i, (p, k) in enumerate(zip(prices, kinds))]


# ── ZigZag ───────────────────────────────────────────────────────────────────────
def test_zigzag_detects_known_pivots():
    pivot_prices = [100, 120, 110, 150, 130, 160]
    series = _build(pivot_prices, steps=10)
    pivots = W.zigzag_pivots(series, threshold_pct=0.05)
    got = [round(p["price"], 2) for p in pivots]
    assert got == [float(x) for x in pivot_prices]
    assert [p["kind"] for p in pivots] == ["L", "H", "L", "H", "L", "H"]
    # последний пивот — текущий экстремум, ещё не подтверждён разворотом
    assert pivots[-1]["confirmed"] is False
    assert pivots[0]["confirmed"] is True


def test_zigzag_ignores_subthreshold_noise():
    # мелкие колебания < порога не создают пивотов
    series = _build([100, 103, 101, 104, 160], steps=8)  # 3% шум, затем большой ход
    pivots = W.zigzag_pivots(series, threshold_pct=0.05)
    prices = [round(p["price"], 1) for p in pivots]
    assert 103 not in prices and 101 not in prices and 104 not in prices
    assert round(pivots[-1]["price"], 1) == 160.0


def test_zigzag_errors():
    with pytest.raises(ValueError):
        W.zigzag_pivots([100.0])
    with pytest.raises(ValueError):
        W.zigzag_pivots([100.0, 110.0], threshold_pct=0)
    with pytest.raises(ValueError):
        W.zigzag_pivots([100.0, 110.0], threshold_pct=1.5)


# ── Фибоначчи ─────────────────────────────────────────────────────────────────────
def test_fib_retracement():
    assert W.fib_retracement(100, 120, 110) == 0.5
    assert W.fib_retracement(100, 120, 100) == 1.0
    assert W.fib_retracement(100, 120, 120) == 0.0
    assert W.fib_retracement(100, 100, 100) is None  # нулевой ход


def test_nearest_fib():
    assert W.nearest_fib(0.62)["level"] == 0.618
    assert W.nearest_fib(1.6)["level"] == 1.618
    assert W.nearest_fib(None)["level"] is None


# ── Импульс: валидный и три нарушения по отдельности ──────────────────────────────
def test_impulse_valid_up():
    pivots = _pivs([100, 120, 110, 150, 130, 160], ["L", "H", "L", "H", "L", "H"])
    res = W.label_impulse(pivots)
    assert res["direction"] == "up"
    assert res["valid"] is True
    assert res["violations"] == []
    # волна 2: ретрейс (120-110)/(120-100)=... здесь по длинам L2/L1=10/20=0.5
    w2 = next(w for w in res["waves"] if w["wave"] == 2)
    assert w2["retrace_of_w1"] == 0.5
    assert w2["fib"]["level"] == 0.5


def test_impulse_valid_down():
    pivots = _pivs([200, 180, 190, 150, 170, 140], ["H", "L", "H", "L", "H", "L"])
    res = W.label_impulse(pivots)
    assert res["direction"] == "down"
    assert res["valid"] is True


def test_impulse_R1_violation():
    # волна 2 откатила ЗА начало волны 1 (P2 <= P0)
    pivots = _pivs([100, 120, 95, 150, 130, 160], ["L", "H", "L", "H", "L", "H"])
    res = W.label_impulse(pivots)
    assert res["valid"] is False
    assert any(v.startswith("R1") for v in res["violations"])


def test_impulse_R2_violation_wave3_shortest():
    # L1=20, L3=16, L5=48 → волна 3 самая короткая; R1 и R3 не нарушены
    pivots = _pivs([100, 120, 112, 128, 122, 170], ["L", "H", "L", "H", "L", "H"])
    res = W.label_impulse(pivots)
    assert any(v.startswith("R2") for v in res["violations"])
    assert not any(v.startswith("R1") for v in res["violations"])
    assert not any(v.startswith("R3") for v in res["violations"])


def test_impulse_R3_overlap_violation():
    # P4=118 заходит в территорию волны 1 (P1=120) → перекрытие
    pivots = _pivs([100, 120, 110, 150, 118, 160], ["L", "H", "L", "H", "L", "H"])
    res = W.label_impulse(pivots)
    assert any(v.startswith("R3") for v in res["violations"])
    assert not any(v.startswith("R1") for v in res["violations"])


def test_impulse_requires_six_pivots():
    with pytest.raises(ValueError):
        W.label_impulse(_pivs([100, 120, 110], ["L", "H", "L"]))


# ── Коррекция ABC ─────────────────────────────────────────────────────────────────
def test_label_correction():
    pivots = _pivs([100, 130, 112, 118], ["L", "H", "L", "H"])
    res = W.label_correction(pivots)
    assert res["A"]["len"] == 30.0
    assert res["B"]["retrace_of_A"] == 0.6      # 18/30
    assert res["C"]["ratio_to_A"] == 0.2        # 6/30


def test_label_correction_requires_four():
    with pytest.raises(ValueError):
        W.label_correction(_pivs([100, 130, 112], ["L", "H", "L"]))


# ── Полная разметка ───────────────────────────────────────────────────────────────
def test_wave_markup_full():
    series = _build([100, 120, 110, 150, 130, 160], steps=10)
    m = W.wave_markup(series, threshold_pct=0.05)
    assert m["n_pivots"] == 6
    assert m["impulse_last"]["valid"] is True
    assert "correction_last" in m
    assert m["n_bars"] == len(series)
    assert "ambiguity_note" in m  # П8: код не выдаёт «единственно верный» счёт


def test_wave_markup_insufficient_data():
    m = W.wave_markup([100, 101, 102], threshold_pct=0.05)
    assert "нет данных" in m["note"]
    assert m["pivots"] == []


def test_wave_markup_few_pivots_notes_gap():
    # плавный рост без разворотов → мало пивотов, импульс честно не строится
    series = list(np.linspace(100, 160, 40))
    m = W.wave_markup(series, threshold_pct=0.05)
    assert m["n_pivots"] < 6
    assert "нет данных" in m["impulse_last"]["note"]
