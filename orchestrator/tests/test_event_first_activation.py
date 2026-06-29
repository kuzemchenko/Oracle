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


def test_money_kind_fail_closed():
    # F0#2: деньги ТОЛЬКО при явном УСТОЯЛА; всё прочее → провизорный (fail-closed гейт §11/П10)
    assert EF._money_kind("УСТОЯЛА") == "cascade_money"
    assert EF._money_kind("РАЗБИТА") == "cascade_provisional"
    assert EF._money_kind("ВЕТО") == "cascade_provisional"
    assert EF._money_kind(None) == "cascade_provisional"          # не судили → НЕ в деньги
    assert EF._money_kind("ОШИБКА_СУДА") == "cascade_provisional"  # сбой суда → НЕ в деньги
    assert EF._money_kind("ПРОПУСК") == "cascade_provisional"      # нет котировки → НЕ в деньги
    assert EF._money_kind({"исход": "УСТОЯЛА"}) == "cascade_money"


# ── Решение D, вариант 3: полный §8-контур по гейту (пережившие слепой суд money-идеи) ──
def test_money_kind_procedural_veto_demotes():
    # вердикт судьи УСТОЯЛА, но полный §8-контур дал процедурное вето (§6) → money→провизорный
    assert EF._money_kind({"исход": "УСТОЯЛА", "процедурное_вето": True}) == "cascade_provisional"
    assert EF._money_kind({"исход": "УСТОЯЛА", "процедурное_вето": False}) == "cascade_money"


def _patch_deep(monkeypatch, *, timing="ВОВРЕМЯ", manip=3):
    from orchestrator import context as _C
    from orchestrator import synthesis as _SY
    from orchestrator import agents as _A
    # F0#4: единый ключ score_block_threshold (шкала балла 0–10, дефолт 7.0)
    monkeypatch.setattr(_C, "_load_yaml",
                        lambda *_a, **_k: {"manipulation": {"score_block_threshold": 7.0}, "timing": {},
                                           "non_obviousness": {}})
    def _agent(aid, *_a, **_k):
        j = {"d_timeliness": {"вердикт": timing},
             "d_anti_manipulation": {"балл": manip},
             "c_non_obviousness": {"вердикт": "ОК"}}.get(aid, {})
        return {"ok": True, "judgment": j}
    monkeypatch.setattr(_A, "call_agent", _agent)
    monkeypatch.setattr(_SY, "run_risk", lambda *_a, **_k: {"ok": True, "judgment": {"риск": "ок"}})
    monkeypatch.setattr(_SY, "synthesize_report",
                        lambda *_a, **_k: {"ok": True, "judgment": {"поля": {"п1": "тезис"}}})


def test_deep_report_clean_idea_no_veto(monkeypatch):
    _patch_deep(monkeypatch, timing="ВОВРЕМЯ", manip=3)   # < порога 7 (шкала 0–10)
    cand = {"актив": "GEV.US", "направление": "лонг", "тезис": "t", "школа": "каскад",
            "дело_каскада": {}, "разрешимость": None}
    debate = {"вердикт": {"исход": "УСТОЯЛА", "вероятность_судьи": 0.62},
              "реплики": {"критик": {"ok": True, "judgment": {"c": 1}},
                          "судья": {"ok": True, "judgment": {"j": 1}}}}
    deep = EF._deep_report_money(cand, debate, ctx={"quotes": {}, "indicators": {}, "news": []},
                                client=None)
    assert deep["процедурное_вето"] is False
    assert deep["отчёт_§8"]["поля"]["п1"] == "тезис"
    assert deep["качество"]["тайминг"]["вердикт"] == "ВОВРЕМЯ"


def test_deep_report_trap_timing_triggers_veto(monkeypatch):
    _patch_deep(monkeypatch, timing="ЛОВУШКА", manip=3)
    cand = {"актив": "GEV.US", "направление": "лонг", "тезис": "t", "школа": "каскад"}
    debate = {"вердикт": {"исход": "УСТОЯЛА", "вероятность_судьи": 0.62}, "реплики": {}}
    deep = EF._deep_report_money(cand, debate, ctx={"quotes": {}, "indicators": {}, "news": []},
                                client=None)
    assert deep["процедурное_вето"] is True
    assert "ЛОВУШКА" in deep["причина_вето"]


def test_deep_report_high_manipulation_triggers_veto(monkeypatch):
    _patch_deep(monkeypatch, timing="ВОВРЕМЯ", manip=8)    # ≥ порога 7 (шкала 0–10)
    cand = {"актив": "GEV.US", "направление": "лонг", "тезис": "t"}
    debate = {"вердикт": {"исход": "УСТОЯЛА"}, "реплики": {}}
    deep = EF._deep_report_money(cand, debate, ctx={"quotes": {}, "indicators": {}, "news": []},
                                client=None)
    assert deep["процедурное_вето"] is True
    assert "манипул" in deep["причина_вето"].lower()
