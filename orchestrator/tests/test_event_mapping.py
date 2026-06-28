# -*- coding: utf-8 -*-
"""Тесты авто-привязки news-кластеров к инструментам (orchestrator/event_mapping.py, долг №3)."""
from orchestrator import event_mapping as EM


UNIVERSE = {"themes": {
    "ai_power": {"proxy_etf": "VRT.US", "related": ["GEV.US", "CLF.US"], "cascade_chain": "x"},
    "spacex": {"proxy_etf": "SPCX.US", "structural": True},
}}


def test_match_cluster_to_known_theme():
    cl = {"keywords": ["datacenter", "transformer", "grid"], "sample": "AI datacenter power demand surges"}
    theme, overlap = EM.match_cluster_to_theme(cl, UNIVERSE)
    assert theme == "ai_power" and overlap > 0


def test_unmatched_cluster_returns_none():
    cl = {"keywords": ["glacier", "iceberg", "ocean"], "sample": "Glaciers reshape deep sea"}
    theme, overlap = EM.match_cluster_to_theme(cl, UNIVERSE)
    assert theme is None and overlap == 0


def test_verify_tickers_filters_fakes_and_illiquid():
    def checker(t):
        return {"REAL.US": {"avg_volume": 5_000_000, "last": 50},
                "THIN.US": {"avg_volume": 1000, "last": 5}}.get(t)  # FAKE.US → None
    out = EM.verify_tickers(["REAL.US", "THIN.US", "FAKE.US"], checker)
    assert [v["ticker"] for v in out] == ["REAL.US"]   # выдумка и неликвид отсеяны


class _FakeClient:
    def __init__(self, text):
        self._t = text
    def complete(self, role, system, user, *, agent_id, output_kind, exclude_family=None):
        return {"text": self._t}


def test_proposal_chains_builds_chain_for_funnel():
    # B2.5: новость вне реестра тем → карта картографа → схема цепочки (chain+anchor) для воронки
    draft = ('{"событие":"Дефицит трансформаторов","первый_порядок":"энергооборудование",'
             '"каскад":[{"порядок":2,"узел":"трансформаторы","тикеры":["GEV.US"],"чокпоинт":false},'
             '{"порядок":3,"узел":"GOES-сталь","тикеры":["CLF.US"],"чокпоинт":true}],'
             '"обоснование":"дефицит","уверенность":"средняя"}')

    def checker(t):
        return {"avg_volume": 500000, "last": 100.0}      # все ликвидны

    clusters = [{"keywords": ["transformer", "shortage"], "sample": "transformer shortage", "salience": 9}]
    chains = EM.proposal_chains(clusters, {"themes": {}}, _FakeClient(draft), checker, max_map=5)
    assert len(chains) == 1
    c = chains[0]
    assert c["anchor"] == "GEV.US"                         # узел минимального порядка (2)
    assert [n["order"] for n in c["chain"]["nodes"]] == [2, 3]
    assert c["событие"] == "Дефицит трансформаторов"
    assert c["mapped"]["kind"] == "proposed"


def test_map_cluster_proposes_and_verifies():
    draft = ('{"событие":"Бум X","первый_порядок":"A","каскад":'
             '[{"порядок":3,"узел":"редкий металл","тикеры":["REAL.US","FAKE.US"],"чокпоинт":true}],'
             '"обоснование":"...","уверенность":"средняя"}')
    client = _FakeClient(draft)
    checker = lambda t: {"avg_volume": 3_000_000, "last": 10} if t == "REAL.US" else None
    cl = {"keywords": ["xboom"], "sample": "X boom"}
    m = EM.map_cluster(cl, UNIVERSE, client, checker)
    assert m["kind"] == "proposed" and m["tradable"]
    assert m["verified_nodes"][0]["verified_tickers"][0]["ticker"] == "REAL.US"  # FAKE отсеян


def test_proposed_map_gets_tectonic_score_and_far_node():
    # долг №4: предложенная карта скорится по T, выбирает дальний чокпоинт-узел
    draft = ('{"событие":"Бум Y","первый_порядок":"A","каскад":'
             '[{"порядок":2,"узел":"оборудование","тикеры":["EQ.US"],"чокпоинт":false},'
             '{"порядок":3,"узел":"редкий металл","тикеры":["MET.US"],"чокпоинт":true}],'
             '"обоснование":"...","уверенность":"средняя"}')
    client = _FakeClient(draft)
    checker = lambda t: {"avg_volume": 2_000_000, "last": 10}   # все реальны/ликвидны
    m = EM.map_cluster({"keywords": ["yboom"], "sample": "Y boom"}, UNIVERSE, client, checker)
    assert m["tectonic"] is not None
    far = m["tectonic"]["best_far_node"]
    assert far["order"] == 3 and "MET.US" in far["instruments"] and far["chokepoint"]
    assert 0 <= m["tectonic"]["tectonic_potential"] <= 1
    # лаги неизвестны → ось L = 0 (окно входа не откалибровано), честно
    assert m["tectonic"]["lag_window_days"] == 0


def test_map_cluster_no_map_when_empty_cascade():
    client = _FakeClient('{"событие":"неясно","каскад":[],"обоснование":"нет переноса"}')
    m = EM.map_cluster({"keywords": ["z"], "sample": "z"}, UNIVERSE, client, lambda t: None)
    assert m["kind"] == "no_map"


def test_stage_proposal_writes_jsonl(tmp_path):
    import json
    p = tmp_path / "proposed_themes.jsonl"
    mapped = {"cluster": {"keywords": ["xboom"]},
              "draft": {"событие": "Бум X", "уверенность": "средняя", "обоснование": "..."},
              "verified_nodes": [{"порядок": 3, "узел": "металл", "чокпоинт": True,
                                  "verified_tickers": [{"ticker": "REAL.US"}]}]}
    rec = EM.stage_proposal(mapped, "2026-06-14T00:00:00+00:00", path=p)
    assert rec["узлы"][0]["тикеры"] == ["REAL.US"]
    assert "НЕ торгуется" in rec["статус"]
    assert json.loads(p.read_text(encoding="utf-8").strip())["событие"] == "Бум X"
