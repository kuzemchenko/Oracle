# -*- coding: utf-8 -*-
"""Пометка записи журнала прогнозов как недействительной (владелец 14.11: фальшивая EQT от теста).
Журнал прогнозов не меняется; пометка — отдельный append-only файл; сверка не выносит исход, счёт не видит."""
import json

import pytest

from mathlib import sealing as SEAL


def _spec(asset="EQT.US"):
    return {"kind": "chat_idea", "asset": asset, "direction": "above", "threshold": 58.0,
            "resolve_by": "2026-12-23T20:00:00+00:00", "price_source": f"EODHD close {asset}"}


def test_void_marks_without_touching_journal(tmp_path):
    pp = tmp_path / "predictions.jsonl"
    r1 = SEAL.seal(_spec(), path=pp)
    r2 = SEAL.seal(_spec("DHT.US"), path=pp)
    before = pp.read_bytes()
    m = SEAL.void(r1["hash"], "тестовая запись", "eqt пометь как тестовую", predictions_path=pp)
    assert pp.read_bytes() == before and m["asset"] == "EQT.US"
    assert SEAL.voided_hashes(pp) == {r1["hash"]}
    assert [r["hash"] for r in SEAL.drop_voided(SEAL.read_predictions(pp), pp)] == [r2["hash"]]
    ok, bad = SEAL.verify_all(pp)
    assert ok and not bad


def test_void_needs_owner_words_existing_hash_and_once(tmp_path):
    pp = tmp_path / "predictions.jsonl"
    r = SEAL.seal(_spec(), path=pp)
    with pytest.raises(ValueError):
        SEAL.void(r["hash"], "тест", "", predictions_path=pp)
    with pytest.raises(ValueError):
        SEAL.void("0" * 64, "тест", "слова", predictions_path=pp)
    SEAL.void(r["hash"], "тест", "слова", predictions_path=pp)
    with pytest.raises(ValueError):
        SEAL.void(r["hash"], "тест", "слова", predictions_path=pp)


def test_resolve_skips_voided(tmp_path, monkeypatch):
    from orchestrator import resolve as RES
    pp, op = tmp_path / "predictions.jsonl", tmp_path / "outcomes.jsonl"
    r = SEAL.seal(dict(_spec(), resolve_by="2026-01-02T20:00:00+00:00"), path=pp)
    SEAL.void(r["hash"], "тест", "слова", predictions_path=pp)
    monkeypatch.setattr(RES, "DB", tmp_path / "нет.db")
    out = RES.run_resolve(write=True, predictions_path=pp, outcomes_path=op)
    assert not (op.exists() and op.read_text(encoding="utf-8").strip())
    assert json.dumps(out, ensure_ascii=False, default=str).count(r["hash"][:12]) == 0
