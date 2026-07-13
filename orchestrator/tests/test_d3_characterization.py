# -*- coding: utf-8 -*-
"""Characterization-тесты этапа Д3 (гейт дорожной карты «Поисковый движок»):

  (а) calibrate-режим НЕ зависит от условного оценивателя — печати байт-в-байт совпадают,
      даже если модуль conditional отравлен (calibrate его не импортирует и не касается);
  (б) node_cascade/cascade_from_quotes без включения with_conditional меняются ТОЛЬКО
      аддитивно (новый алиас sensitivity_unconditional; sensitivity_conditional отсутствует),
      решения ворот (sealable/probability/amplitude/причина) — прежние поля, прежние значения.

Полный байт-дифф calibrate и B4-протокола против кода master — в отчёте этапа
(ops/reports/d3_conditional/REPORT.md, секция characterization)."""
import datetime
import json
import sys
import pathlib
import types

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import calibrate as CAL     # noqa: E402
from mathlib import cascade as CAS            # noqa: E402

NOW = datetime.datetime(2026, 6, 12, 13, 0, 0, tzinfo=datetime.timezone.utc)
DB = ROOT / "storage" / "oracle.db"

# старый контракт node_cascade (до Д3) — эти ключи и их значения обязаны сохраниться
LEGACY_NODE_KEYS = {"sealable", "причина", "sensitivity", "shock", "amplitude",
                    "reliability_r2", "слабый_перенос", "probability",
                    "horizon_days", "threshold"}


@pytest.mark.skipif(not DB.exists(), reason="нет storage/oracle.db")
def test_calibrate_predictions_independent_of_conditional_module():
    import sqlite3
    con = sqlite3.connect(DB)
    try:
        before = json.dumps(CAL.build_calibration_predictions(con, "t", now_dt=NOW),
                            ensure_ascii=False, sort_keys=True)
        # отравляем модуль условного оценивателя: любое обращение к нему упало бы
        poisoned = types.ModuleType("mathlib.calibration.conditional")

        def _boom(*a, **k):  # noqa: ANN001
            raise AssertionError("calibrate не должен касаться conditional (Д3 аддитивен)")
        poisoned.estimate_pair = _boom
        poisoned.estimate_pair_symbols = _boom
        saved = sys.modules.get("mathlib.calibration.conditional")
        sys.modules["mathlib.calibration.conditional"] = poisoned
        try:
            after = json.dumps(CAL.build_calibration_predictions(con, "t", now_dt=NOW),
                               ensure_ascii=False, sort_keys=True)
        finally:
            if saved is not None:
                sys.modules["mathlib.calibration.conditional"] = saved
            else:
                sys.modules.pop("mathlib.calibration.conditional", None)
    finally:
        con.close()
    assert before == after       # байт-в-байт: calibrate не зависит от Д3


def _series():
    rng = np.random.default_rng(5)
    src = rng.normal(0.0, 0.01, 400)
    node = 0.8 * src + rng.normal(0.0, 0.003, 400)
    return src, node


def test_node_cascade_default_is_additive_only():
    src, node = _series()
    res = CAS.node_cascade(src, node, shock=-0.05, horizon_days=5, lag=0)
    # новые ключи строго аддитивны: только алиас; conditional по умолчанию НЕ считается (lazy)
    assert set(res) == LEGACY_NODE_KEYS | {"sensitivity_unconditional"}
    assert "sensitivity_conditional" not in res
    assert res["sensitivity_unconditional"] is res["sensitivity"]   # тот же объект, не пересчёт
    # решения ворот определяются прежними полями и совпадают с прямым безусловным расчётом
    sens = CAS.node_sensitivity(src, node, lag=0)
    assert res["sensitivity"] == sens
    assert res["amplitude"] == round(CAS.node_amplitude(sens["beta"], -0.05), 6)
    assert res["probability"] == CAS.node_probability(
        CAS.node_amplitude(sens["beta"], -0.05), sens["resid_std"], 5, 0.0)
    assert res["sealable"] is True


def test_node_cascade_no_data_branch_additive():
    res = CAS.node_cascade([0.01] * 5, [0.01] * 5, shock=0.01, horizon_days=5)
    assert res["sealable"] is False
    assert res["sensitivity"] is None
    assert res["sensitivity_unconditional"] is None
    assert "sensitivity_conditional" not in res


def test_node_cascade_with_conditional_attaches_record():
    src, node = _series()
    res = CAS.node_cascade(src, node, shock=-0.05, horizon_days=5, lag=0,
                           with_conditional=True,
                           conditional_kwargs={"train": 120, "test": 60, "step": 60})
    rec = res["sensitivity_conditional"]
    assert rec is not None
    assert rec["status"] in ("установлено", "не установлено")
    for key in ("n_episodes", "lag_window", "wf_established", "tier", "провенанс"):
        assert key in rec
    # безусловное измерение при этом НЕ изменилось
    assert res["sensitivity"] == CAS.node_sensitivity(src, node, lag=0)


def test_cascade_from_quotes_signature_defaults_off():
    """with_conditional по умолчанию False — боевые печати event_first/replay не меняются
    без явного переключения (переключение — этап Э4, не Д3)."""
    import inspect
    sig = inspect.signature(CAS.cascade_from_quotes)
    assert sig.parameters["with_conditional"].default is False
    sig2 = inspect.signature(CAS.node_cascade)
    assert sig2.parameters["with_conditional"].default is False
