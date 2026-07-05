# -*- coding: utf-8 -*-
"""Тесты B4 (§R4.5) — форвард-тест рёбер библиотеки: активация по порогу (подпись 05.07),
однозвенная атрибуция, внутри-трековый дедуп, герметичный третий трек, корм промоушена.
Герметично: in-memory quotes, tmp-журналы, фикс-бета вместо on_the_fly."""
import datetime
import json
import pathlib
import sqlite3
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import edge_forward as EFW      # noqa: E402
from orchestrator import cascade_build as CB      # noqa: E402
from orchestrator import forecast as FC           # noqa: E402
from orchestrator import resolve as RES           # noqa: E402
from mathlib import sealing as SEAL               # noqa: E402
from ops import promote_edges as PE               # noqa: E402

NOW = datetime.datetime(2026, 7, 5, 7, 30, tzinfo=datetime.timezone.utc)


def _dates(n):
    return [f"2026-{3 + i // 28:02d}-{i % 28 + 1:02d}" for i in range(n)]


def _db(series):
    """series: {sym: [close,...]} — одинаковая дато-сетка (выравнивание для изоляции R²)."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, open REAL, high REAL, low REAL,"
                " close REAL, adjusted_close REAL, volume INTEGER)")
    for sym, closes in series.items():
        for d, c in zip(_dates(len(closes)), closes):
            con.execute("INSERT INTO quotes (symbol, date, close, adjusted_close, volume)"
                        " VALUES (?,?,?,?,1000000)", (sym, d, c, c))
    return con


def _series_shocked(n=110, base=100.0, jump=1.06):
    """Плоский ряд с шоком +6% ВНУТРИ окна реакции §R2.1 (последние 5 баров) — источник активирован."""
    return [base] * (n - 5) + [base * jump] * 5


def _series_quiet(n=110, base=50.0):
    """Малошумный ряд без тренда (терминал: σ>0, реализованное ≈ 0)."""
    return [base + 0.05 * (i % 5) for i in range(n)]


def _sens_yaml(tmp_path, edges):
    p = tmp_path / "sens.yaml"
    p.write_text(yaml.safe_dump({"sensitivities": edges, "chain_sensitivities": []},
                                allow_unicode=True), encoding="utf-8")
    return p


def _fix_beta(monkeypatch, beta=0.5):
    monkeypatch.setattr(CB.SEN, "on_the_fly",
                        lambda up, down, lag=0, db=None, asof=None: {
                            "источник": up, "узел": down, "lag": lag, "pinned": True,
                            "beta_pinned": beta, "r2": 0.6, "n_obs": 100, "provenance": "тест"})


def test_edge_library_dedup_direction_selfloop(tmp_path):
    p = _sens_yaml(tmp_path, [
        {"источник": "A.US", "узел": "B.US", "lag": 0},
        {"источник": "A.US", "узел": "B.US", "lag": 0},      # дубль
        {"источник": "B.US", "узел": "A.US", "lag": 0},      # обратное направление — ДРУГОЕ ребро
        {"источник": "A.US", "узел": "B.US", "lag": 30},     # другой лаг — другое ребро
        {"источник": "C.US", "узел": "C.US", "lag": 0},      # само-звено — артефакт, не связь
        {"источник": None, "узел": "D.US", "lag": 0},        # битая запись
    ])
    lib = EFW.edge_library(p)
    assert lib == [{"from": "A.US", "to": "B.US", "lag": 0},
                   {"from": "A.US", "to": "B.US", "lag": 30},
                   {"from": "B.US", "to": "A.US", "lag": 0}]


def test_activated_edge_seals_single_link(tmp_path, monkeypatch):
    _fix_beta(monkeypatch)
    con = _db({"AAA.US": _series_shocked(), "BBB.US": _series_quiet()})
    sens = _sens_yaml(tmp_path, [{"источник": "AAA.US", "узел": "BBB.US", "lag": 0}])
    preds = tmp_path / "pred.jsonl"
    r = EFW.run_edge_forward(write=False, seal=True, con=con, now_dt=NOW,
                             sens_path=sens, predictions_path=preds)
    assert r["итоги"]["запечатано"] == 1, r["рёбра"]
    rec = SEAL.read_predictions(preds)[0]
    assert rec["kind"] == "edge_forward"
    assert rec["edge_key"] == "AAA.US->BBB.US@lag0"
    assert len(rec["cascade_path"]) == 1                       # однозвенный — атрибутируется ребру
    assert rec["cascade_path"][0]["from"] == "AAA.US" and rec["cascade_path"][0]["to"] == "BBB.US"
    assert rec["asset"] == "BBB.US" and rec["direction"] in ("above", "below")
    assert rec["probability"] is not None and rec["resolve_by"] > NOW.isoformat()
    assert "§R4.5" in rec["spec_ref"]


def test_quiet_source_not_sealed_with_reason(tmp_path, monkeypatch):
    # источник без шока → нет неотыгранного хода/под порогом — прогноз НЕ печатается (подпись 05.07)
    _fix_beta(monkeypatch)
    con = _db({"AAA.US": _series_quiet(base=100.0), "BBB.US": _series_quiet()})
    sens = _sens_yaml(tmp_path, [{"источник": "AAA.US", "узел": "BBB.US", "lag": 0}])
    preds = tmp_path / "pred.jsonl"
    r = EFW.run_edge_forward(write=False, seal=True, con=con, now_dt=NOW,
                             sens_path=sens, predictions_path=preds)
    assert r["итоги"]["запечатано"] == 0
    assert not preds.exists()                                  # журнал не тронут
    assert r["рёбра"] and r["рёбра"][0]["статус"] in ("спит", "пропуск")
    assert r["рёбра"][0].get("причина")                        # причина журналируется (П8)


def test_rerun_same_day_is_idempotent(tmp_path, monkeypatch):
    _fix_beta(monkeypatch)
    con = _db({"AAA.US": _series_shocked(), "BBB.US": _series_quiet()})
    sens = _sens_yaml(tmp_path, [{"источник": "AAA.US", "узел": "BBB.US", "lag": 0}])
    preds = tmp_path / "pred.jsonl"
    r1 = EFW.run_edge_forward(write=False, seal=True, con=con, now_dt=NOW,
                              sens_path=sens, predictions_path=preds)
    r2 = EFW.run_edge_forward(write=False, seal=True, con=con, now_dt=NOW,
                              sens_path=sens, predictions_path=preds)
    assert r1["итоги"]["запечатано"] == 1
    assert r2["итоги"]["запечатано"] == 0 and r2["итоги"]["дубль_пропущен"] == 1
    assert len(SEAL.read_predictions(preds)) == 1


def test_no_cross_track_dedup_collision(tmp_path, monkeypatch):
    # Та же ставка (актив/направление/порог/срок) в ДРУГОМ треке — НЕ дубль: треки герметичны.
    _fix_beta(monkeypatch)
    con = _db({"AAA.US": _series_shocked(), "BBB.US": _series_quiet()})
    sens = _sens_yaml(tmp_path, [{"источник": "AAA.US", "узел": "BBB.US", "lag": 0}])
    preds = tmp_path / "pred.jsonl"
    EFW.run_edge_forward(write=False, seal=True, con=con, now_dt=NOW,
                         sens_path=sens, predictions_path=preds)
    rec = SEAL.read_predictions(preds)[0]
    prov = {k: rec[k] for k in ("asset", "direction", "threshold", "resolve_by",
                                "price_source", "probability")}
    prov["kind"] = "cascade_provisional"
    assert FC.seal_prediction(prov, path=preds) is not None    # провизорный НЕ погашен (kind в identity)
    # а РАЗНЫЕ рёбра к одному терминалу с равными полями ставки — тоже разные прогнозы
    spec2 = {k: rec[k] for k in ("kind", "asset", "direction", "threshold", "resolve_by",
                                 "price_source", "probability", "cascade_path")}
    spec2["edge_key"] = "CCC.US->BBB.US@lag0"
    assert SEAL.seal(spec2, path=preds, dedup_fields=EFW.DEDUP_FIELDS) is not None
    # но ИДЕНТИЧНОЕ ребро — дубль
    spec3 = dict(spec2)
    assert SEAL.seal(spec3, path=preds, dedup_fields=EFW.DEDUP_FIELDS) is None


def test_resolve_third_track_and_promotion_feed(tmp_path, monkeypatch):
    # созревший edge_forward → resolve кладёт исход в СВОЙ трек (не money, не провизорный),
    # а promote_edges видит его как корм однозвенной атрибуции ребра.
    preds = tmp_path / "pred.jsonl"
    outs = tmp_path / "out.jsonl"
    dbfile = tmp_path / "q.db"
    con = sqlite3.connect(dbfile)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL)")
    con.execute("INSERT INTO quotes (symbol, date, close) VALUES ('BBB.US','2026-07-01',51.0)")
    con.commit(); con.close()
    monkeypatch.setattr(RES, "DB", dbfile)
    spec = {"kind": "edge_forward", "run_id": "b4_t", "asset": "BBB.US", "direction": "above",
            "threshold": 50.0, "resolve_by": "2026-07-01T20:00:00+00:00",
            "price_source": "EODHD close BBB.US", "probability": 0.7,
            "cascade_path": [{"from": "AAA.US", "to": "BBB.US", "lag": 0,
                              "tier": "C", "beta_fullsample": 0.5}],
            "edge_key": "AAA.US->BBB.US@lag0", "spec_ref": "тест"}
    assert SEAL.seal(spec, path=preds, dedup_fields=EFW.DEDUP_FIELDS)
    s = RES.run_resolve(write=True, predictions_path=preds, outcomes_path=outs)
    assert s["edge_forward_трек"]["исходов"] == 1              # третий трек видит исход
    assert s["edge_forward_трек"]["brier"] is not None
    assert s["brier"] is None                                  # §11/money не тронут
    assert s["провизорный_трек"]["исходов"] == 0               # и провизорный не подмешан
    rows, stats = PE.collect_rows(preds, outs)
    assert len(rows) == 1 and rows[0]["edge_key"] == "AAA.US->BBB.US@lag0"
    assert rows[0]["outcome"] == 1 and rows[0]["probability"] == 0.7


def test_seal_flag_off_never_touches_journal(tmp_path, monkeypatch):
    _fix_beta(monkeypatch)
    con = _db({"AAA.US": _series_shocked(), "BBB.US": _series_quiet()})
    sens = _sens_yaml(tmp_path, [{"источник": "AAA.US", "узел": "BBB.US", "lag": 0}])
    preds = tmp_path / "pred.jsonl"
    called = []
    monkeypatch.setattr(EFW.SEAL, "seal", lambda *a, **k: called.append(1))
    r = EFW.run_edge_forward(write=False, seal=False, con=con, now_dt=NOW,
                             sens_path=sens, predictions_path=preds)
    assert not called and not preds.exists()
    assert r["итоги"]["запечатано"] == 1                       # dry: посчитано «к печати», не в журнал
    assert r["рёбра"][-1]["статус"] == "к_печати (dry)"
