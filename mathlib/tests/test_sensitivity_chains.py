# -*- coding: utf-8 -*-
"""Этап 3 §3c: чувствительности по звеньям конкретных компаний на лету.

Проверяем извлечение звеньев из карты цепочек (чистая функция) и честный гейт «нет данных» (П8)
для отсутствующего тикера — без выдуманной беты.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mathlib.calibration import sensitivity as SEN   # noqa: E402
import ops.calibrate_sensitivities as DRV            # noqa: E402


def test_chain_edge_pairs_extraction():
    doc = {"chains": [{
        "id": "demo",
        "nodes": [
            {"order": 1, "instruments": ["AAA.US"]},
            {"order": 2, "instruments": ["BBB.US", "CCC.US"]},
            {"order": 3, "instruments": ["DDD.US"]},
        ],
        "edges": [{"from": 1, "to": 2, "lag_days": 30}, {"from": 2, "to": 3, "lag_days": 60}],
    }]}
    pairs = DRV._chain_edge_pairs(doc)
    assert len(pairs) == 2
    assert pairs[0] == {"chain_id": "demo", "источник": "AAA.US", "узел": "BBB.US",
                        "lag_days": 30, "звено": "ord1→ord2"}
    # представительный инструмент узла = первый торгуемый
    assert pairs[1]["источник"] == "BBB.US" and pairs[1]["узел"] == "DDD.US"


def test_chain_edge_pairs_skips_nodes_without_instruments():
    doc = {"chains": [{"id": "x", "nodes": [
        {"order": 1, "instruments": []}, {"order": 2, "instruments": ["B.US"]}],
        "edges": [{"from": 1, "to": 2, "lag_days": 10}]}]}
    assert DRV._chain_edge_pairs(doc) == []


def test_on_the_fly_no_data_for_absent_symbol():
    # заведомо отсутствующий тикер → честный «нет данных», без беты (П8)
    rec = SEN.on_the_fly("ZZZZ.US", "BNO.US", lag=0)
    assert rec["pinned"] is None
    assert rec["beta_pinned"] is None
    assert "нет данных" in rec["provenance"]
    assert rec["источник"] == "ZZZZ.US" and rec["узел"] == "BNO.US"
