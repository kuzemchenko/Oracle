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


# --- Ревью 2026-07-04: якорь ловит усечение хвоста; легаси-вписывание в цепь ловится ---
def test_anchor_detects_tail_truncation(tmp_path):
    # Известная дыра: удаление ПОСЛЕДНИХ строк оставляет валидную цепь — verify_all слеп.
    path = tmp_path / "p.jsonl"
    _seal_n(path, 3)
    ok_a, why = s.verify_anchor(path)
    assert ok_a and why is None
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")   # усечение хвоста
    ok, bad = s.verify_all(path)
    assert ok                                                        # цепь по построению не видит
    ok_a, why = s.verify_anchor(path)
    assert not ok_a and "усечение" in why                            # якорь видит


def test_anchor_missing_on_nonempty_journal_fails_closed(tmp_path):
    path = tmp_path / "p.jsonl"
    _seal_n(path, 2)
    s._anchor_path(path).unlink()                                    # «пропавший» якорь
    ok_a, why = s.verify_anchor(path)
    assert not ok_a and "якорь отсутствует" in why
    # пустой журнал без якоря — легитимно (журнал ещё не заводили)
    ok_b, _ = s.verify_anchor(tmp_path / "empty.jsonl")
    assert ok_b


def test_init_anchor_migrates_existing_journal(tmp_path):
    # Миграция боевого журнала: журнал существует, якоря ещё нет → init_anchor, дальше всё ок.
    path = tmp_path / "p.jsonl"
    _seal_n(path, 2)
    s._anchor_path(path).unlink()
    info = s.init_anchor(path)
    assert info["count"] == 2
    ok_a, _ = s.verify_anchor(path)
    assert ok_a


def test_forged_legacy_record_in_tail_is_flagged(tmp_path):
    # HIGH-2 ревью: сфабрикованная «легаси»-запись (без prev_hash, с самосогласованным hash),
    # дописанная ПОСЛЕ начала цепочки, раньше проходила verify_all. Теперь — битая.
    path = tmp_path / "p.jsonl"
    _seal_n(path, 2)
    forged = _good(); forged["threshold"] = 555.0
    forged["sealed_at"] = "2026-07-01T00:00:00+00:00"
    forged["hash"] = s._content_hash(forged)                         # hash есть, prev_hash нет
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(forged, ensure_ascii=False) + "\n")
    ok, bad = s.verify_all(path)
    assert not ok and 2 in bad


def test_append_chained_generic_journal(tmp_path):
    # Универсальная цепочка для outcomes-подобного журнала: rec_hash/prev_rec_hash,
    # поле hash остаётся свободным под ссылку на прогноз; легаси-префикс допустим.
    path = tmp_path / "outcomes.jsonl"
    legacy = {"hash": "pred-hash-1", "outcome": 1}                   # запись до миграции: без rec_hash
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    r1 = s.append_chained(path, {"hash": "pred-hash-2", "outcome": 0})
    r2 = s.append_chained(path, {"hash": "pred-hash-3", "outcome": 1})
    assert r1["prev_rec_hash"] == s.GENESIS_PREV_HASH                # легаси-хвост без rec_hash → GENESIS
    assert r2["prev_rec_hash"] == r1["rec_hash"]
    ok, bad = s.verify_chain(path, hash_field="rec_hash", prev_field="prev_rec_hash", require_hash=False)
    assert ok and bad == []
    ok_a, _ = s.verify_anchor(path, hash_field="rec_hash")
    assert ok_a
    # правка chained-записи ловится
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1]); rec["outcome"] = 1
    lines[1] = json.dumps(rec, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, bad = s.verify_chain(path, hash_field="rec_hash", prev_field="prev_rec_hash", require_hash=False)
    assert not ok and 1 in bad


def test_seal_dedup_same_bet_not_resealed(tmp_path):
    # Идемпотентность перезапуска: та же ставка (asset+direction+threshold+resolve_by) → None,
    # журнал не растёт; другая ставка — запечатывается.
    path = tmp_path / "p.jsonl"
    fields = ("asset", "direction", "threshold", "resolve_by")
    r1 = s.seal(_good(), path=path, sealed_at="2026-06-11T00:00:00+00:00", dedup_fields=fields)
    assert r1 is not None
    r2 = s.seal(_good(), path=path, sealed_at="2026-06-11T09:00:00+00:00", dedup_fields=fields)
    assert r2 is None                                              # дубль (иное время не спасает)
    assert len(s.read_predictions(path)) == 1
    other = _good(); other["threshold"] = 91.0
    r3 = s.seal(other, path=path, sealed_at="2026-06-11T09:01:00+00:00", dedup_fields=fields)
    assert r3 is not None and len(s.read_predictions(path)) == 2


def test_hash_stable_regardless_of_key_order(tmp_path):
    # тот же контент, другой порядок ключей → тот же hash (канонизация sort_keys)
    a = _good()
    b = {k: a[k] for k in reversed(list(a.keys()))}
    ra = s.seal(a, path=tmp_path / "a.jsonl", sealed_at="2026-06-11T00:00:00+00:00")
    rb = s.seal(b, path=tmp_path / "b.jsonl", sealed_at="2026-06-11T00:00:00+00:00")
    assert ra["hash"] == rb["hash"]
