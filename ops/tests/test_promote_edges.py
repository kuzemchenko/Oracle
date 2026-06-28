# -*- coding: utf-8 -*-
"""Драйвер форвард-промоушена рёбер (ops/promote_edges.py): джойн predictions↔outcomes по hash,
атрибуция ОДНОЗВЕННЫХ путей ребру, гейт §10. Только временные журналы — без боевого журнала (П16)."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import promote_edges as PE                        # noqa: E402
from mathlib.calibration import forward_promotion as FP    # noqa: E402


def _write(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _make_journals(tmp_path, n_hits, n_total, *, kind="cascade_provisional",
                   path_len=1, with_outcomes=True):
    edge = {"from": "A.US", "to": "B.US", "lag": 30, "tier": "C", "beta_fullsample": 1.2}
    cascade_path = [edge] * path_len
    preds, outs = [], []
    for i in range(n_total):
        h = f"hash{i}"
        preds.append({"hash": h, "kind": kind, "asset": "B.US", "probability": 0.8,
                      "cascade_path": cascade_path})
        if with_outcomes:
            outs.append({"hash": h, "kind": kind, "asset": "B.US",
                         "probability": 0.8, "outcome": 1 if i < n_hits else 0})
    p_path = tmp_path / "preds.jsonl"
    o_path = tmp_path / "outs.jsonl"
    _write(p_path, preds)
    _write(o_path, outs)
    return str(p_path), str(o_path)


def test_collect_rows_attributes_single_edge(tmp_path):
    pp, op = _make_journals(tmp_path, n_hits=24, n_total=30)
    rows, stats = PE.collect_rows(pp, op)
    assert len(rows) == 30
    assert stats["провизорных_исходов_однозвенных"] == 30
    assert rows[0]["edge_key"] == FP.edge_key("A.US", "B.US", 30)
    assert rows[0]["beta_fullsample"] == 1.2


def test_multi_edge_paths_skipped(tmp_path):
    pp, op = _make_journals(tmp_path, n_hits=30, n_total=30, path_len=2)
    rows, stats = PE.collect_rows(pp, op)
    assert rows == []                              # композитный путь → не атрибутируется ребру
    assert stats["многозвенных_пропущено"] == 30


def test_pending_without_outcome_skipped(tmp_path):
    pp, op = _make_journals(tmp_path, n_hits=0, n_total=10, with_outcomes=False)
    rows, stats = PE.collect_rows(pp, op)
    assert rows == []
    assert stats["ещё_pending"] == 10


def test_evaluate_promotes_strong_edge(tmp_path):
    pp, op = _make_journals(tmp_path, n_hits=24, n_total=30)   # 80% hit, N=30
    res = PE.evaluate(pp, op)
    key = FP.edge_key("A.US", "B.US", 30)
    assert res["promotions"][key]["promote"] is True
    assert res["n_promote"] == 1
    assert res["promotions"][key]["from"] == "A.US"


def test_evaluate_blocks_below_n(tmp_path):
    pp, op = _make_journals(tmp_path, n_hits=20, n_total=20)   # идеал, но N<30
    res = PE.evaluate(pp, op)
    assert res["n_promote"] == 0


def test_money_kind_predictions_ignored(tmp_path):
    pp, op = _make_journals(tmp_path, n_hits=30, n_total=30, kind="cascade_money")
    rows, _ = PE.collect_rows(pp, op)
    assert rows == []                              # промоушен питается ТОЛЬКО провизорным треком
