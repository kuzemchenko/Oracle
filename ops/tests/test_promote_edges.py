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
    # F0#5: РЕАЛЬНЫЙ скилл, а не base-rate-монетка — сбалансированные исходы (base≈0.5) + направленный
    # прогноз верен в n_hits случаях из n_total при уверенности p_conf=0.8 (иначе промоушен блокируется).
    n_up = round(n_total * 0.5)
    preds, outs = [], []
    for i in range(n_total):
        h = f"hash{i}"
        y = 1 if i < n_up else 0
        predicted_up = (y == 1) if (i < n_hits) else (y == 0)   # верен в первых n_hits
        p = 0.8 if predicted_up else 0.2
        preds.append({"hash": h, "kind": kind, "asset": "B.US", "probability": p,
                      "cascade_path": cascade_path})
        if with_outcomes:
            outs.append({"hash": h, "kind": kind, "asset": "B.US", "probability": p, "outcome": y})
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


# ── Э4(ж) / долг B4(а): дедуп ЭПИЗОДА шока в корме промоушена ──────────────────────
def _episode_journals(tmp_path, episodes, *, edge=("A.US", "B.US", 0), field="episode"):
    """По одной печати на дату эпизода (одно ребро). field: episode | sealed_at (легаси-прокси)."""
    cascade_path = [{"from": edge[0], "to": edge[1], "lag": edge[2]}]
    preds, outs = [], []
    for i, ep in enumerate(episodes):
        h = f"h{i}"
        rec = {"hash": h, "kind": "edge_forward", "asset": edge[1], "probability": 0.8,
               "cascade_path": cascade_path,
               # разные ставки (порог/срок) — cross-track дедуп их НЕ склеит, работает именно эпизод
               "direction": "above", "threshold": 50.0 + i, "resolve_by": f"2026-08-{i+1:02d}"}
        rec[field] = ep if field == "episode" else f"{ep}T07:30:00+00:00"
        preds.append(rec)
        outs.append({"hash": h, "outcome": 1})
    pp, op = tmp_path / "p.jsonl", tmp_path / "o.jsonl"
    _write(pp, preds)
    _write(op, outs)
    return str(pp), str(op)


def test_episode_dedup_same_episode_counted_once(tmp_path):
    """Печати одного ребра с эпизодами ближе EPISODE_GAP_DAYS — ОДНО свидетельство (keep-first)."""
    pp, op = _episode_journals(tmp_path, ["2026-07-01", "2026-07-03", "2026-07-06"])
    rows, stats = PE.collect_rows(pp, op)
    assert len(rows) == 1
    assert stats["дубль_эпизода_шока"] == 2


def test_episode_dedup_distinct_episodes_kept(tmp_path):
    pp, op = _episode_journals(tmp_path, ["2026-07-01", "2026-07-10", "2026-07-20"])
    rows, stats = PE.collect_rows(pp, op)
    assert len(rows) == 3 and stats["дубль_эпизода_шока"] == 0


def test_episode_dedup_legacy_without_episode_not_merged(tmp_path):
    """Э4-ревью (HIGH): легаси-записи БЕЗ поля episode (только sealed_at) НЕ склеиваются —
    явной episode-идентичности в старых данных нет, sealed_at ≠ эпизод (две независимые печати
    в одном календарном зазоре — не один эпизод). Обе проходят как самостоятельные свидетельства."""
    pp, op = _episode_journals(tmp_path, ["2026-07-01", "2026-07-02"], field="sealed_at")
    rows, stats = PE.collect_rows(pp, op)
    assert len(rows) == 2 and stats["дубль_эпизода_шока"] == 0


def test_episode_dedup_no_date_passes_through_p8(tmp_path):
    """Ни episode, ни sealed_at → дату не выдумываем (П8), строки проходят как есть."""
    pp, op = _make_journals(tmp_path, n_hits=24, n_total=30)   # фикстуры без дат
    rows, stats = PE.collect_rows(pp, op)
    assert len(rows) == 30 and stats["дубль_эпизода_шока"] == 0


def test_episode_dedup_per_edge_isolated(tmp_path):
    """Эпизоды дедупятся ВНУТРИ ребра: одинаковые даты у РАЗНЫХ рёбер не склеиваются."""
    pp1, op1 = _episode_journals(tmp_path, ["2026-07-01"])
    preds = [json.loads(l) for l in open(pp1, encoding="utf-8")]
    outs = [json.loads(l) for l in open(op1, encoding="utf-8")]
    preds.append({"hash": "hx", "kind": "edge_forward", "asset": "C.US", "probability": 0.8,
                  "cascade_path": [{"from": "X.US", "to": "C.US", "lag": 0}],
                  "direction": "above", "threshold": 10.0, "resolve_by": "2026-08-09",
                  "episode": "2026-07-01"})
    outs.append({"hash": "hx", "outcome": 1})
    pp = tmp_path / "p2.jsonl"; op = tmp_path / "o2.jsonl"
    _write(pp, preds); _write(op, outs)
    rows, stats = PE.collect_rows(str(pp), str(op))
    assert len(rows) == 2 and stats["дубль_эпизода_шока"] == 0
