# -*- coding: utf-8 -*-
"""Тесты запечатывания (§9, П16, инвариант 3): разрешимость, hash, append-only, ловля подделки."""
import sys
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mathlib import sealing as s  # noqa: E402


def _good():
    return {
        "asset": "Brent front-month",
        "direction": "above",
        "threshold": 90.0,
        "resolve_by": "2026-06-30T20:00:00+00:00",
        "price_source": "EODHD continuous front future",
        "probability": 0.62,
    }


# --- §9: стандарт разрешимости -------------------------------------------
def test_resolvable_accepts_full_prediction():
    assert s.is_resolvable(_good())
    assert s.validate_resolvable(_good()) == []


@pytest.mark.parametrize("drop", ["asset", "direction", "threshold", "resolve_by", "price_source"])
def test_missing_required_field_is_unresolvable(drop):
    p = _good()
    del p[drop]
    problems = s.validate_resolvable(p)
    assert problems and any(drop in x for x in problems)


def test_vague_prediction_rejected():
    # «нефть вырастет» — без порога/направления/срока (§9): неразрешимо
    assert not s.is_resolvable({"asset": "oil", "narrative": "нефть вырастет"})


def test_bad_direction_and_probability_rejected():
    p = _good(); p["direction"] = "вверх"
    assert any("direction" in x for x in s.validate_resolvable(p))
    p = _good(); p["probability"] = 1.5
    assert any("probability" in x for x in s.validate_resolvable(p))


# --- seal(): append, timestamp+hash --------------------------------------
def test_seal_appends_one_line(tmp_path):
    path = tmp_path / "predictions.jsonl"
    rec = s.seal(_good(), path=path, sealed_at="2026-06-11T00:00:00+00:00")
    assert rec["sealed_at"] == "2026-06-11T00:00:00+00:00"
    assert rec["hash"]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["hash"] == rec["hash"]


def test_seal_is_append_only_never_overwrites(tmp_path):
    path = tmp_path / "predictions.jsonl"
    s.seal(_good(), path=path, sealed_at="2026-06-11T00:00:00+00:00")
    p2 = _good(); p2["threshold"] = 95.0
    s.seal(p2, path=path, sealed_at="2026-06-11T00:01:00+00:00")
    assert len(s.read_predictions(path)) == 2


def test_seal_refuses_unresolvable(tmp_path):
    path = tmp_path / "predictions.jsonl"
    with pytest.raises(ValueError):
        s.seal({"asset": "oil"}, path=path)
    assert not path.exists()  # в журнал ничего не попало (П8)


# --- подделка задним числом ловится --------------------------------------
def test_tampering_detected(tmp_path):
    path = tmp_path / "predictions.jsonl"
    rec = s.seal(_good(), path=path, sealed_at="2026-06-11T00:00:00+00:00")
    assert s.verify_seal(rec)
    tampered = dict(rec); tampered["threshold"] = 1.0       # подмена порога
    assert not s.verify_seal(tampered)
    tampered2 = dict(rec); tampered2["sealed_at"] = "2025-01-01T00:00:00+00:00"  # подмена времени
    assert not s.verify_seal(tampered2)


def test_verify_all_flags_corrupted_journal(tmp_path):
    path = tmp_path / "predictions.jsonl"
    s.seal(_good(), path=path, sealed_at="2026-06-11T00:00:00+00:00")
    p2 = _good(); p2["threshold"] = 88.0
    s.seal(p2, path=path, sealed_at="2026-06-11T00:01:00+00:00")
    ok, bad = s.verify_all(path)
    assert ok and bad == []
    # вручную портим вторую строку (имитация правки в обход seal)
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    recs[1]["threshold"] = 9999.0
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    ok, bad = s.verify_all(path)
    assert not ok and bad == [1]


def test_hash_stable_regardless_of_key_order(tmp_path):
    # тот же контент, другой порядок ключей → тот же hash (канонизация sort_keys)
    a = _good()
    b = {k: a[k] for k in reversed(list(a.keys()))}
    ra = s.seal(a, path=tmp_path / "a.jsonl", sealed_at="2026-06-11T00:00:00+00:00")
    rb = s.seal(b, path=tmp_path / "b.jsonl", sealed_at="2026-06-11T00:00:00+00:00")
    assert ra["hash"] == rb["hash"]
