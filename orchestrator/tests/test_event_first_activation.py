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


def test_price_signal_syms_filters_candidates():
    # Д1-Вариант2: активация каскадов идёт по КАНДИДАТУ (топ по значимости), а НЕ по строгому FDR.
    scan = {"сигналы": [
        {"символ": "GEV.US", "кандидат": True, "сигнал_после_FDR": False},  # FDR не прошёл — но кандидат
        {"символ": "X.US", "кандидат": False, "сигнал_после_FDR": False},   # не кандидат → мимо
        {"символ": None, "кандидат": True},
    ]}
    assert EF._price_signal_syms(scan) == ["GEV.US"]


def test_theme_for_chain():
    assert EF._theme_for_chain("demo", UNIVERSE) == "ai_power"
    assert EF._theme_for_chain("нет_такой", UNIVERSE) is None


def test_activation_by_node_price_signal():
    # новостей нет (тема не сматчится) → активация ТОЛЬКО по ценовому сигналу на узле GEV
    scan = {"новостные_события": [], "сигналы": [{"символ": "GEV.US", "кандидат": True}]}
    act = EF.activated_chains(scan, UNIVERSE, CHAINS, EF._price_signal_syms(scan))
    assert len(act) == 1
    assert act[0]["chain"]["id"] == "demo"
    assert act[0]["anchor"] == "VRT.US"                    # якорь = узел минимального порядка
    assert any("ценовой сигнал" in r for r in act[0]["причины"])


def test_no_activation_without_theme_or_signal():
    scan = {"новостные_события": [], "сигналы": []}
    assert EF.activated_chains(scan, UNIVERSE, CHAINS, []) == []


def test_variant2_candidate_is_superset_of_fdr():
    """stage-review 14.07: всё, что прошло строгий FDR, ОБЯЗАНО быть кандидатом — даже если p выше
    порога заметности CAND_P_MAX (BH отвергает вплоть до q_max=0.1 > 0.05) или вытеснено бы капом."""
    from orchestrator import event_scan as ES
    stat = [
        {"вид": "price", "символ": "A", "_p_raw": 0.08, "сигнал_после_FDR": True},   # p>0.05, но FDR+
        {"вид": "price", "символ": "B", "_p_raw": 0.30, "сигнал_после_FDR": False},  # не заметен, не FDR
    ]
    ES._mark_candidates(stat)
    assert stat[0]["кандидат"] is True    # прошёл FDR → кандидат несмотря на p=0.08>0.05
    assert stat[1]["кандидат"] is False   # ни заметности, ни FDR


def test_variant2_shock_sources_news_before_price(monkeypatch):
    """stage-review 14.07: новостной прокси СОБЫТИЯ дня идёт в источники шока ПЕРЕД ценовыми
    кандидатами — дорогой контур якорится на событии, а не на крупнейшем движении."""
    from orchestrator import event_first as EF2
    monkeypatch.setattr(EF2.EM, "match_cluster_to_theme", lambda cl, uni: ("th", 1.0))
    monkeypatch.setattr(EF2.U, "is_sealable", lambda s, con=None: True)
    uni = {"themes": {"th": {"proxy_etf": "OIH.US"}}}
    scan = {"сигналы": [{"символ": "AAA.US", "кандидат": True, "q_value": 0.2}],
            "новостные_события": [{"ключи": ["hormuz"], "пример": "strike"}]}
    src = EF2._shock_sources(scan, uni, con=None, max_sources=8)
    assert src.index("OIH.US") < src.index("AAA.US")   # новостной прокси впереди ценового


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


def test_variant2_trend_candidate_routes_to_proxy(monkeypatch):
    """Этап2: трендовый кандидат → тема → proxy_etf доходит до узлов активации (закрытие долга Д1-В2).
    Незамапленный трендовый кандидат честно выпадает (нет привязки к инструменту)."""
    from orchestrator import event_first as EF2
    monkeypatch.setattr(EF2.EM, "match_cluster_to_theme",
                        lambda cl, uni: ("oil", 1.0)
                        if "lithium" in " ".join(cl.get("keywords", [])) else (None, 0))
    uni = {"themes": {"oil": {"proxy_etf": "USO.US"}}}
    scan = {"сигналы": [
        {"вид": "trend", "ключ": "lithium supply", "кандидат": True},   # мапится → USO
        {"вид": "trend", "ключ": "random noise", "кандидат": True},     # не мапится → выпадает
        {"вид": "trend", "ключ": "lithium supply", "кандидат": False},  # не кандидат → игнор
        {"вид": "price", "символ": "AAA.US", "кандидат": True},
    ]}
    assert EF2._trend_proxy_syms(scan, uni) == [("lithium supply", "USO.US")]
    # боевой путь берёт каналы РАЗДЕЛЬНО: ценовой узел из _price_signal_syms, трендовый — из proxy
    assert "AAA.US" in EF2._price_signal_syms(scan)          # ценовой кандидат — узел
    assert "USO.US" in {p for _k, p in EF2._trend_proxy_syms(scan, uni)}  # трендовый — свой канал


def test_activation_reason_distinguishes_price_and_trend(monkeypatch):
    """Этап2 stage-review (П8, регрессия): активация цепочки трендовым прокси помечается КАК ТРЕНДОВАЯ,
    а не ложно «ценовой сигнал на узле». Ценовая — остаётся ценовой. Приоритет цены при совпадении."""
    from orchestrator import event_first as EF2
    monkeypatch.setattr(EF2, "_theme_for_chain", lambda cid, uni: None)   # активация только по узлам
    scan = {"новостные_события": []}
    uni = {"themes": {}}
    chains = [{"id": "c1", "nodes": [{"order": 1, "instruments": ["TREND.US"]},
                                     {"order": 2, "instruments": ["PRICE.US"]}]}]
    acts = EF2.activated_chains(scan, uni, chains, ["PRICE.US"], ["TREND.US"])
    assert len(acts) == 1
    reasons = " ".join(acts[0]["причины"])
    assert "ценовой сигнал на узле(ах): ['PRICE.US']" in reasons
    assert "трендовый интерес" in reasons and "TREND.US" in reasons
    assert "ценовой сигнал на узле(ах): ['TREND.US']" not in reasons   # тренд НЕ выдаётся за цену

    # узел в обоих каналах → метится как ценовой (приоритет), не дублируется в трендовый
    chains2 = [{"id": "c2", "nodes": [{"order": 1, "instruments": ["BOTH.US"]}]}]
    acts2 = EF2.activated_chains(scan, uni, chains2, ["BOTH.US"], ["BOTH.US"])
    r2 = " ".join(acts2[0]["причины"])
    assert "ценовой сигнал на узле(ах): ['BOTH.US']" in r2 and "трендовый интерес" not in r2


def test_daily_debate_alert_fires_and_dedups(tmp_path):
    """Этап2 (2.4): расход за день > ориентира → алерт в notices; дедуп на день; mock/чужой день не в счёт."""
    import datetime, json as _j
    from orchestrator import event_first as EF2
    now = datetime.datetime(2026, 7, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    limits = tmp_path / "limits.yaml"
    limits.write_text("budget:\n  daily_debate_alert_usd: 5\n  tokens_usd_month: 500\n", encoding="utf-8")
    costs = tmp_path / "costs.jsonl"
    costs.write_text("\n".join(_j.dumps(x) for x in [
        {"ts": "2026-07-15T09:00:00Z", "mode": "live", "cost_usd": 4.0},
        {"ts": "2026-07-15T10:00:00Z", "mode": "live", "cost_usd": 3.0},   # итого 7 > 5
        {"ts": "2026-07-14T10:00:00Z", "mode": "live", "cost_usd": 9.0},   # другой день — не в счёт
        {"ts": "2026-07-15T11:00:00Z", "mode": "mock", "cost_usd": 9.0},   # mock — не в счёт
    ]) + "\n", encoding="utf-8")
    notices = tmp_path / "notices.jsonl"
    res = EF2._daily_debate_alert("live", now, costs_log=costs, limits_path=limits, notices=notices)
    assert res["over"] and abs(res["spent_today"] - 7.0) < 1e-6 and res["cap"] == 5
    assert "[дневной-расход 2026-07-15]" in notices.read_text(encoding="utf-8")
    n1 = len(notices.read_text(encoding="utf-8").splitlines())
    EF2._daily_debate_alert("live", now, costs_log=costs, limits_path=limits, notices=notices)  # повтор
    assert len(notices.read_text(encoding="utf-8").splitlines()) == n1                          # без дубля
    assert EF2._daily_debate_alert("mock", now, costs_log=costs, limits_path=limits, notices=notices) is None
