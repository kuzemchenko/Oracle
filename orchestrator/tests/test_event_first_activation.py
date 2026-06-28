# -*- coding: utf-8 -*-
"""Этап 4 / дыра №1: широкая активация авторских цепочек — по теме ИЛИ ценовому сигналу узла.

Тестируем детерминированный путь (сигнал на узле) и чистые хелперы. Тематический путь зависит от
живого матчера EM.match_cluster_to_theme и проверяется интеграционным мок-прогоном.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_first as EF   # noqa: E402

UNIVERSE = {"themes": {"ai_power": {"cascade_chain": "demo"}}}
CHAINS = [{"id": "demo", "nodes": [
    {"order": 1, "instruments": ["VRT.US"]},
    {"order": 2, "instruments": ["GEV.US"]},
]}]


def test_price_signal_syms_filters_fdr():
    scan = {"сигналы": [
        {"символ": "GEV.US", "сигнал_после_FDR": True},
        {"символ": "X.US", "сигнал_после_FDR": False},     # не прошёл FDR
        {"символ": None, "сигнал_после_FDR": True},
    ]}
    assert EF._price_signal_syms(scan) == ["GEV.US"]


def test_theme_for_chain():
    assert EF._theme_for_chain("demo", UNIVERSE) == "ai_power"
    assert EF._theme_for_chain("нет_такой", UNIVERSE) is None


def test_activation_by_node_price_signal():
    # новостей нет (тема не сматчится) → активация ТОЛЬКО по ценовому сигналу на узле GEV
    scan = {"новостные_события": [], "сигналы": [{"символ": "GEV.US", "сигнал_после_FDR": True}]}
    act = EF.activated_chains(scan, UNIVERSE, CHAINS, EF._price_signal_syms(scan))
    assert len(act) == 1
    assert act[0]["chain"]["id"] == "demo"
    assert act[0]["anchor"] == "VRT.US"                    # якорь = узел минимального порядка
    assert any("ценовой сигнал" in r for r in act[0]["причины"])


def test_no_activation_without_theme_or_signal():
    scan = {"новостные_события": [], "сигналы": []}
    assert EF.activated_chains(scan, UNIVERSE, CHAINS, []) == []


def test_proposal_ideas_promotes_far_node_company_ranked():
    # РЕШЕНИЕ A: предложения картографа (вне тем) → research-идеи на конкретной дальней компании
    proposals = [
        {"событие": "ставка ФРС", "ключи": ["fed", "rates"], "узлы": [],
         "тектонический_потенциал": 0.4,
         "целевой_дальний_узел": {"order": 2, "instruments": ["XHB.US"], "chokepoint": False, "priced": 0.5}},
        {"событие": "дроновый удар", "ключи": ["drone", "defense"], "узлы": [],
         "тектонический_потенциал": 0.8,
         "целевой_дальний_узел": {"order": 3, "instruments": ["LMT.US"], "chokepoint": True, "priced": 0.2}},
        {"событие": "без инструмента", "целевой_дальний_узел": {"instruments": []}},   # пропуск (П8)
    ]
    ideas = EF._proposal_ideas(proposals)
    assert [i["актив"] for i in ideas] == ["LMT.US", "XHB.US"]   # ранг по тект. потенциалу
    assert ideas[0]["research"] is True and ideas[0]["чокпоинт"] is True
    assert ideas[0]["источник_идеи"].startswith("LLM-картограф")


def test_money_kind_demotes_broken_by_blind_judge():
    # перенаправленный контур: слепой суд разбил money-идею → демотируем в провизорный (не к §11)
    assert EF._money_kind("РАЗБИТА") == "cascade_provisional"
    assert EF._money_kind("ВЕТО") == "cascade_provisional"
    assert EF._money_kind("УСТОЯЛА") == "cascade_money"
    assert EF._money_kind(None) == "cascade_money"          # не судили → остаётся money
