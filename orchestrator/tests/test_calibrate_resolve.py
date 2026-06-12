# -*- coding: utf-8 -*-
"""Тесты калибровочного режима (§17.3/§23.2в) и сверки исходов (§10.10).

Калибровка: строит §9-разрешимые недельные прогнозы с РАЗНЫМИ вероятностями (0.31/0.5/0.69)
и ЭМПИРИЧЕСКИМИ base rate; mock НЕ запечатывает (П16). Сверка: дозревший прогноз сверяется
с фактическим close из БД ЧИСТЫМ кодом, исход пишется в ОТДЕЛЬНЫЙ outcomes.jsonl (predictions
не трогаем); недозревший — pending (П8); повторный прогон не дублирует исход (append-only join по hash)."""
import datetime
import json
import sqlite3
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import calibrate as CAL    # noqa: E402
from orchestrator import resolve as RES      # noqa: E402
from mathlib import sealing as SEAL          # noqa: E402

NOW = datetime.datetime(2026, 6, 12, 13, 0, 0, tzinfo=datetime.timezone.utc)


def test_calibrate_builds_resolvable_varied_predictions():
    con = sqlite3.connect(ROOT / "storage" / "oracle.db")
    preds = CAL.build_calibration_predictions(con, "t", now_dt=NOW)
    con.close()
    assert len(preds) >= 12                      # ≥4 актива × 3 порога
    for p in preds:
        assert not SEAL.validate_resolvable(p)   # все §9-разрешимы
        assert p["prob_model"] == "gaussian_baseline_realized_vol"
        assert p["kind"] == "calibration"
    probs = {p["probability"] for p in preds}
    assert len(probs) >= 3                        # разные вероятности → есть спред для Brier


def test_calibrate_mock_does_not_seal():
    before = len(SEAL.read_predictions())
    out = CAL.run_calibrate(mode="mock", write=False, now_dt=NOW)
    assert out["запечатано"] == 0
    assert len(SEAL.read_predictions()) == before  # боевой журнал не тронут


def test_resolve_matured_prediction(tmp_path):
    # дозревший прогноз по BNO.US: порог заведомо ниже факта → outcome=1 (above)
    con = sqlite3.connect(ROOT / "storage" / "oracle.db")
    row = con.execute("SELECT date, close FROM quotes WHERE symbol='BNO.US' AND date>='2026-06-09' "
                      "ORDER BY date ASC LIMIT 1").fetchone()
    con.close()
    obs_date, obs_close = row[0], float(row[1])

    ppath = tmp_path / "preds.jsonl"
    opath = tmp_path / "outs.jsonl"
    pred = {"kind": "calibration", "asset": "BNO.US", "direction": "above",
            "threshold": round(obs_close - 5.0, 4), "resolve_by": "2026-06-09T20:00:00+00:00",
            "price_source": "EODHD close BNO.US", "probability": 0.6}
    SEAL.seal(pred, path=str(ppath))

    out = RES.run_resolve(write=True, predictions_path=str(ppath), outcomes_path=str(opath))
    assert out["сверено_сейчас"] == 1
    recs = RES.read_outcomes(str(opath))
    assert recs[0]["outcome"] == 1
    assert recs[0]["observed_value"] == obs_close

    # повторный прогон не дублирует (append-only join по hash)
    out2 = RES.run_resolve(write=True, predictions_path=str(ppath), outcomes_path=str(opath))
    assert out2["сверено_сейчас"] == 0
    assert len(RES.read_outcomes(str(opath))) == 1


def test_resolve_pending_when_not_matured(tmp_path):
    ppath = tmp_path / "p.jsonl"
    pred = {"kind": "calibration", "asset": "BNO.US", "direction": "above", "threshold": 50.0,
            "resolve_by": "2027-01-01T20:00:00+00:00", "price_source": "EODHD close BNO.US",
            "probability": 0.5}
    SEAL.seal(pred, path=str(ppath))
    out = RES.run_resolve(write=False, predictions_path=str(ppath), outcomes_path=str(tmp_path / "o.jsonl"))
    assert out["сверено_сейчас"] == 0 and out["ещё_pending"] == 1
