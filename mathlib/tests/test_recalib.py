# -*- coding: utf-8 -*-
"""Тесты V4 — изотоническая рекалибровка (mathlib/calibration/recalib.py).

Закрепляют:
  • PAV: монотонность карты по обеим координатам, восстановление частот на
    калиброванных данных, пустой вход;
  • применение: интерполяция между узлами, честный клип за краями (не экстраполяция),
    пустая карта = тождество;
  • walk-forward: ОТКАЗ при малой выборке; фильтрация мусора (p_raw=None, outcome=None);
    детерминизм бутстрэпа (один seed → байт в байт); вырожденный вход (одна сырая
    вероятность) → спред 0 и «преимущество не значимо»;
  • на сигнальных синтетических данных (исходы следуют p_raw) спред > 0 и
    калиброванный Brier не хуже сырого на OOS.
"""
import random

from mathlib.calibration import recalib as R


def _rows(n_days=40, per_day=8, seed=7, signal=True):
    rng = random.Random(seed)
    rows = []
    for d in range(n_days):
        day = f"2026-{5 + d // 28:02d}-{d % 28 + 1:02d}"
        for _ in range(per_day):
            p = rng.choice([0.30, 0.40, 0.50, 0.60, 0.70])
            true_p = p if signal else 0.48
            rows.append({"p_raw": p, "outcome": 1 if rng.random() < true_p else 0,
                         "sealed_at": day + "T09:00:00+00:00"})
    return rows


def test_pav_fit_monotone_and_recovers_frequencies():
    rows = _rows()
    mapping = R.pav_fit([r["p_raw"] for r in rows], [r["outcome"] for r in rows])
    assert mapping, "непустой вход → непустая карта"
    xs = [m[0] for m in mapping]
    ys = [m[1] for m in mapping]
    assert xs == sorted(xs) and ys == sorted(ys), "изотоника монотонна по обеим осям"
    assert all(0.0 <= y <= 1.0 for y in ys)
    # на сигнальных данных карта различает низ и верх
    assert ys[-1] - ys[0] > 0.10


def test_pav_fit_empty():
    assert R.pav_fit([], []) == []


def test_pav_apply_interpolation_and_clipping():
    mapping = [(0.3, 0.35), (0.7, 0.55)]
    assert R.pav_apply(mapping, 0.5) == 0.45          # линейная середина
    assert R.pav_apply(mapping, 0.1) == 0.35          # клип к левому краю
    assert R.pav_apply(mapping, 0.99) == 0.55         # клип к правому краю
    assert R.pav_apply([], 0.42) == 0.42              # пустая карта = тождество


def test_walk_forward_refuses_small_sample():
    out = R.fit_walk_forward(_rows(n_days=5, per_day=3))
    assert "ОТКАЗ" in out and "< 100" in out["ОТКАЗ"]


def test_walk_forward_filters_garbage_rows():
    rows = _rows()
    rows += [{"p_raw": None, "outcome": 1, "sealed_at": "2026-09-01"},
             {"p_raw": 0.5, "outcome": None, "sealed_at": "2026-09-01"}]
    out = R.fit_walk_forward(rows)
    assert out["n_всего"] == len(rows) - 2


def test_walk_forward_deterministic_and_improves_on_signal():
    rows = _rows()
    a = R.fit_walk_forward(rows)
    b = R.fit_walk_forward(rows)
    assert a == b, "один seed → воспроизводимо байт в байт (Инв#6)"
    assert a["спред_калиброванных_пп"] > 0
    oos = a["OOS"]
    assert oos["brier_калибр"] <= oos["brier_raw"] + 0.01, \
        "на данных с сигналом калибровка не хуже сырых"
    bt = a["бутстрэп_по_дням"]
    assert bt["n_boot"] == 500 and len(bt["спред_пп_CI95"]) == 2


def test_walk_forward_degenerate_single_probability():
    rng = random.Random(3)
    rows = [{"p_raw": 0.4669, "outcome": 1 if rng.random() < 0.46 else 0,
             "sealed_at": f"2026-06-{d % 28 + 1:02d}T09:00:00+00:00"}
            for d in range(200)]
    out = R.fit_walk_forward(rows)
    assert out["узлов_изотоники"] == 1
    assert out["спред_калиброванных_пп"] == 0.0
    assert out["бутстрэп_по_дням"]["преимущество_значимо"] is False, \
        "константа не может «значимо» побить константу"


def test_report_to_yaml_dict_shape():
    out = R.fit_walk_forward(_rows())
    doc = R.report_to_yaml_dict(out, track="provisional",
                                source_note="journal/predictions.jsonl × outcomes.jsonl")
    assert doc["track"] == "provisional"
    assert doc["mapping"] and {"p_raw", "p_cal"} == set(doc["mapping"][0])
    assert "ПРЕДЛОЖЕНИЕ" in doc["статус"]
