# -*- coding: utf-8 -*-
"""Тесты devops-петли-предложителя (ops/devops_loop.py, §R6).

Гермётично: агрегаты террейна и правила предложений — чистые функции. Главное — АВТОНОМИЯ 0:
каждое содержательное предложение помечено «требует подписи» (система сама не применяет).
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import devops_loop as D    # noqa: E402


def test_terrain_metrics_aggregates():
    runs = [
        {"граф_отбор": {"узлов": 10, "отсев_по_критериям": {"ликвидность": 6, "окно": 1},
                        "треки": {"money": 2, "провизорный": 3},
                        "добор_истории": {"скачано": ["A.US", "B.US"]}},
         "каскады_в_компании": [{"chain_id": "c1"}, {"chain_id": "dead", "пропуск": "нет шока"}]},
        {"граф_отбор": {"узлов": 4, "отсев_по_критериям": {"ликвидность": 3},
                        "треки": {"money": 1, "провизорный": 0}, "добор_истории": {"скачано": []}},
         "каскады_в_компании": [{"chain_id": "c1"}]},
    ]
    m = D.terrain_metrics(runs)
    assert m["прогонов"] == 2 and m["узлов_всего"] == 14
    assert m["доля_отсева"]["ликвидность"] == 0.9                     # 9 из 10 отсева
    assert m["цепочки_активаций"] == {"c1": 2, "dead": 0}
    assert m["тикеров_добрано"] == 2 and m["треки"]["провизорный"] == 3


def test_generate_proposals_flags_dominant_gate_and_dead_chain():
    metrics = {"прогонов": 5, "доля_отсева": {"ликвидность": 0.6, "окно": 0.1},
               "цепочки_активаций": {"c1": 3, "dead": 0}}
    props = D.generate_proposals(metrics, {"провизорный": {"исходов": 0, "brier": None}})
    obs = " ".join(p["наблюдение"] for p in props)
    assert "ликвидность" in obs and "dead" in obs
    assert all(p["требует_подписи"] for p in props)                  # автономия 0: всё на подпись


def test_generate_proposals_graduation_when_calibrated():
    props = D.generate_proposals({"доля_отсева": {}, "цепочки_активаций": {}},
                                 {"провизорный": {"исходов": 35, "brier": 0.18}})
    assert any("ВЫПУСК" in p["предложение"] for p in props)          # §R3: выпуск по N≥30


def test_generate_proposals_empty_when_clean():
    props = D.generate_proposals({"доля_отсева": {}, "цепочки_активаций": {}}, {"провизорный": {}})
    assert len(props) == 1 and props[0]["требует_подписи"] is False  # нечего менять — не выдумываем
