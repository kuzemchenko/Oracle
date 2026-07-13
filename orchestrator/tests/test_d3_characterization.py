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
from mathlib.calibration import conditional as _COND   # noqa: E402

COND_TRAIN_TEST = _COND.TRAIN + _COND.TEST

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


def test_conditional_module_poisoning_does_not_affect_default_cascade(monkeypatch):
    """Д3-ревью (LOW 6в) страж-тест: подменяем атрибут mathlib.calibration.conditional отравленным
    модулем — node_cascade БЕЗ with_conditional обязан работать байт-в-байт (он его не трогает)."""
    import mathlib.calibration as MC
    src, node = _series()
    before = json.dumps(CAS.node_cascade(src, node, shock=-0.05, horizon_days=5, lag=0),
                        ensure_ascii=False, sort_keys=True)
    poisoned = types.ModuleType("mathlib.calibration.conditional")

    def _boom(*a, **k):  # noqa: ANN001
        raise AssertionError("conditional не должен вызываться без with_conditional (Д3 аддитивен)")
    poisoned.estimate_pair = _boom
    monkeypatch.setattr(MC, "conditional", poisoned)
    after = json.dumps(CAS.node_cascade(src, node, shock=-0.05, horizon_days=5, lag=0),
                       ensure_ascii=False, sort_keys=True)
    assert before == after


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


def test_no_data_branch_carries_conditional_key_when_requested():
    """Д3-ревью (LOW 6г): ключ sensitivity_conditional присутствует и на отказном пути
    (нет синхронной истории) — когда его явно запросили. По умолчанию — по-прежнему отсутствует."""
    short = [0.01] * 5
    res = CAS.node_cascade(short, short, shock=0.01, horizon_days=5, with_conditional=True,
                           conditional_kwargs={"train": 120, "test": 60, "step": 60})
    assert res["sealable"] is False and res["sensitivity"] is None
    assert "sensitivity_conditional" in res                 # ключ есть даже на «нет данных»
    assert res["sensitivity_conditional"]["status"] == "не установлено"
    res_off = CAS.node_cascade(short, short, shock=0.01, horizon_days=5)
    assert "sensitivity_conditional" not in res_off         # без запроса — не появляется


def test_cascade_from_quotes_conditional_measures_on_long_history(tmp_path):
    """Д3-ревью (HIGH): интеграция раньше была мертва — lookback=400 < train+test=756, условный
    всегда «нет данных». Теперь conditional-ветка берёт свой длинный срез: на 10 годах измерение
    СОСТОИТСЯ (установлено ИЛИ честно не установлено — но НЕ «история N<train+test»)."""
    import sqlite3
    dbfile = tmp_path / "q.db"
    con = sqlite3.connect(dbfile)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, open REAL, high REAL, low REAL,"
                " close REAL, adjusted_close REAL, volume INTEGER)")
    n = 2600                                                 # ~10 лет торговых дней
    rng = np.random.default_rng(9)
    src_px, node_px = [100.0], [50.0]
    for i in range(1, n):
        r = rng.normal(0, 0.01)
        src_px.append(src_px[-1] * (1 + r))
        node_px.append(node_px[-1] * (1 + 0.7 * r + rng.normal(0, 0.004)))  # синхронный перенос
    base = datetime.date(2016, 1, 4)
    for sym, px in (("USO.US", src_px), ("BNO.US", node_px)):
        for i, p in enumerate(px):
            d = (base + datetime.timedelta(days=i)).isoformat()
            con.execute("INSERT INTO quotes (symbol, date, close, adjusted_close, volume)"
                        " VALUES (?,?,?,?,1000000)", (sym, d, p, p))
    con.commit(); con.close()
    res = CAS.cascade_from_quotes("USO.US", -0.05, ["BNO.US"], horizon_days=5, db=dbfile,
                                  with_conditional=True)
    node = res["узлы"][0]
    cond = node["sensitivity_conditional"]
    assert cond is not None
    # ключевое: измерение СОСТОЯЛОСЬ — НЕ отвергнуто по нехватке истории (это и был мёртвый путь)
    assert "история" not in str(cond.get("провенанс", ""))
    assert cond["n_obs"] >= COND_TRAIN_TEST
    assert cond["status"] in ("установлено", "не установлено")
