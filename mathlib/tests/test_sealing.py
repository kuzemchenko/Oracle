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


# --- F2#21: hash-chain ловит удаление/перестановку/вставку ----------------
def _seal_n(path, n):
    recs = []
    for i in range(n):
        p = _good(); p["threshold"] = 90.0 + i
        recs.append(s.seal(p, path=path, sealed_at=f"2026-06-11T00:0{i}:00+00:00"))
    return recs


def test_chain_links_each_record_to_previous(tmp_path):
    path = tmp_path / "p.jsonl"
    recs = _seal_n(path, 3)
    assert recs[0]["prev_hash"] == s.GENESIS_PREV_HASH          # первая → genesis
    assert recs[1]["prev_hash"] == recs[0]["hash"]              # звено
    assert recs[2]["prev_hash"] == recs[1]["hash"]
    ok, bad = s.verify_all(path)
    assert ok and bad == []


def test_chain_detects_deletion(tmp_path):
    path = tmp_path / "p.jsonl"
    _seal_n(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]                                                # удаляем СРЕДНЮЮ запись (в обход seal)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, bad = s.verify_all(path)
    assert not ok and 1 in bad                                 # запись после удалённой рвёт цепочку


def test_chain_detects_reorder(tmp_path):
    path = tmp_path / "p.jsonl"
    _seal_n(path, 3)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1], lines[2] = lines[2], lines[1]                     # перестановка
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, bad = s.verify_all(path)
    assert not ok and bad                                      # перестановка ловится цепочкой


def test_legacy_records_without_prev_hash_stay_valid(tmp_path):
    # Бэк-совместимость: запись БЕЗ prev_hash (как в боевом журнале до F2#21) верифицируется по-старому,
    # а следующая новая seal-запись подхватывает её hash звеном.
    path = tmp_path / "p.jsonl"
    leg = _good(); leg["sealed_at"] = "2026-06-10T00:00:00+00:00"
    leg["hash"] = s._content_hash(leg)                          # легаси: hash без prev_hash
    path.write_text(json.dumps(leg, ensure_ascii=False) + "\n", encoding="utf-8")
    ok, bad = s.verify_all(path)
    assert ok and bad == []                                    # легаси валидна, цепочка не навязана
    new = s.seal(_good(), path=path, sealed_at="2026-06-11T00:00:00+00:00")
    assert new["prev_hash"] == leg["hash"]                      # новая ссылается на легаси
    ok2, _ = s.verify_all(path)
    assert ok2


def test_hash_stable_regardless_of_key_order(tmp_path):
    # тот же контент, другой порядок ключей → тот же hash (канонизация sort_keys)
    a = _good()
    b = {k: a[k] for k in reversed(list(a.keys()))}
    ra = s.seal(a, path=tmp_path / "a.jsonl", sealed_at="2026-06-11T00:00:00+00:00")
    rb = s.seal(b, path=tmp_path / "b.jsonl", sealed_at="2026-06-11T00:00:00+00:00")
    assert ra["hash"] == rb["hash"]
