# -*- coding: utf-8 -*-
"""Тесты герметичности треков seal (B3c §R3, Вариант 2).

ГЛАВНОЕ: провизорный трек (ярус B/C — гипотезы) НЕ протекает в денежные ворота §11 и денежный
Brier — копит СВОЙ Brier отдельно. Плюс seal_spec метит kind по треку и НЕ гейтит по ярусу.
"""
import datetime
import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import resolve as R              # noqa: E402
from orchestrator import cascade_resolve as CR     # noqa: E402
from data import eodhd as E                        # noqa: E402
from mathlib import sealing as SEAL                # noqa: E402


def test_resolve_segments_provisional_from_money_gate(tmp_path):
    preds = tmp_path / "preds.jsonl"; preds.write_text("")     # пусто → новых сверок нет
    out = tmp_path / "outcomes.jsonl"
    rows = [
        {"hash": "a", "probability": 0.7, "outcome": 1, "kind": "funnel_forward"},      # EDGE-деньги
        {"hash": "b", "probability": 0.6, "outcome": 0, "kind": "cascade_money"},       # EDGE-деньги
        {"hash": "z", "probability": 0.5, "outcome": 1, "kind": "calibration"},         # F0#6: НЕ в §11
        {"hash": "c", "probability": 0.9, "outcome": 1, "kind": "cascade_provisional"}, # ПРОВИЗОРНЫЙ
        {"hash": "d", "probability": 0.8, "outcome": 0, "kind": "cascade_provisional"}, # ПРОВИЗОРНЫЙ
    ]
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    # ревью 2026-07-04: outcomes под якорем — легаси-журнал без якоря требует одноразовой миграции
    SEAL.init_anchor(str(out), hash_field="rec_hash")
    res = R.run_resolve(write=False, predictions_path=str(preds), outcomes_path=str(out))

    # F0#6: гейт-270 и денежный Brier — ТОЛЬКО edge-kind (funnel_forward/cascade_money); calibration
    # (baseline-монетка) и провизорные к §11 НЕ приближаются (герметичность треков, алоулист).
    assert res["до_ворот_270"] == 270 - 2                              # a,b — НЕ z(calibration)
    assert res["brier"] == pytest.approx((0.3 ** 2 + 0.6 ** 2) / 2)   # только a,b
    # провизорный трек — отдельно, свой Brier (НЕ подмешан к денежному)
    assert res["провизорный_трек"]["исходов"] == 2
    assert res["провизорный_трек"]["brier"] == pytest.approx((0.1 ** 2 + 0.8 ** 2) / 2)   # только c,d


def _db_with_close(sym, close):
    con = sqlite3.connect(":memory:")
    con.executescript(E.SCHEMA)
    E.upsert(con, sym, [{"date": "2025-01-10", "open": close, "high": close, "low": close,
                         "close": close, "adjusted_close": close, "volume": 1_000_000}])
    return con


def test_seal_spec_tags_track_and_ignores_tier(monkeypatch):
    # герметичность от боевой политики П-1 (сжатие шкалы — свои тесты test_prob_shrink_seal):
    # этот тест проверяет маркировку ТРЕКА и направление, шкала тут — сырая
    from mathlib.calibration import prob_shrink as PS
    monkeypatch.setattr(PS, "load_policy", lambda path=None: None)
    con = _db_with_close("ZZZ.US", 50.0)
    now = datetime.datetime(2026, 6, 21, tzinfo=datetime.timezone.utc)
    # ярус C — НЕ гейтит, идёт в провизорный трек
    prov = {"symbol": "ZZZ.US", "amplitude": 0.04, "probability": 0.7, "tiers": ["C", "C"], "reliability": 0.04}
    spec = CR.seal_spec(prov, kind="cascade_provisional", run_id="t", horizon_days=20, con=con, now_dt=now)
    assert spec is not None
    assert spec["kind"] == "cascade_provisional"
    assert spec["direction"] == "above" and spec["threshold"] == 50.0
    assert spec["probability"] == 0.7                       # above → P как есть

    # ярус A → money трек; шорт (edge<0) → below, вероятность 1-P
    money = {"symbol": "ZZZ.US", "amplitude": -0.02, "probability": 0.7, "tiers": ["A"], "reliability": 0.5}
    spec_m = CR.seal_spec(money, kind="cascade_money", run_id="t", horizon_days=20, con=con, now_dt=now)
    assert spec_m["kind"] == "cascade_money" and spec_m["direction"] == "below"
    assert spec_m["probability"] == pytest.approx(round(1 - 0.7, 4))


def test_seal_spec_skips_zero_amplitude():
    con = _db_with_close("ZZZ.US", 50.0)
    flat = {"symbol": "ZZZ.US", "amplitude": 0.0, "probability": 0.5, "tiers": ["A"]}
    assert CR.seal_spec(flat, kind="cascade_money", run_id="t", horizon_days=20, con=con) is None
