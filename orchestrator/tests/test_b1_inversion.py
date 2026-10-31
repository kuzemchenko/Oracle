# -*- coding: utf-8 -*-
"""B1 (разворот А+B, «делай сразу все» 15.07): генерация идей 2-го порядка / ИНВЕРСИИ.

Владелец: «ты не те идеи ищешь; может LLM не видит идеи 2-го порядка — нестабильная нефть → РОСТ акций».
Движок уже умеет инверсию (отрицательная бета → шорт/лонг обратного знака); не хватало ГЕНЕРАЦИИ.
Проверяем детерминированно (без LLM/сети):
  • картограф-промпты требуют искать инверсию/контринтуитивного победителя (регресс-страховка, чтобы
    директива не исчезла молча);
  • флаг «инверсия» из карты картографа переживает адаптер цепочки и доходит до узла-фактов;
  • тезис исследовательского суда помечает инверсную связь (плюс к неочевидности).
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_mapping as EM        # noqa: E402
from orchestrator import event_first as EF          # noqa: E402


def test_mapper_prompt_requires_inversion():
    s = EM.MAPPER_SYSTEM
    assert "ИНВЕРСИ" in s and "контринтуитив" in s.lower()
    assert '"инверсия"' in s          # контракт несёт флаг


def test_generator_prompt_file_has_inversion_block():
    """Собранный промпт генератора несёт директиву поиска инверсии/2-го порядка (регресс-страховка)."""
    p = ROOT / "agents" / "prompts" / "e_generator.md"
    txt = p.read_text(encoding="utf-8")
    assert "ИНВЕРСИЮ И 2-Й ПОРЯДОК" in txt and "обратного эффекта" in txt.lower()


def test_proposal_to_chain_preserves_inversion_flag():
    mapped = {"draft": {"событие": "Атака на танкер у Ормуза"},
              "verified_nodes": [
                  {"порядок": 2, "узел": "перекройка маршрутов", "чокпоинт": True, "инверсия": True,
                   "verified_tickers": [{"ticker": "FRO.US", "type": "Common Stock"}]},
                  {"порядок": 3, "узел": "очевидный нефтяник", "чокпоинт": False,
                   "verified_tickers": [{"ticker": "XOM.US", "type": "Common Stock"}]}]}
    chain = EM._proposal_to_chain(mapped)
    by_order = {nd["order"]: nd for nd in chain["nodes"]}
    assert by_order[2]["инверсия"] is True        # инверсный узел помечен
    assert by_order[3]["инверсия"] is False       # обычный — нет (bool по умолчанию)


def test_node_to_facts_threads_inversion(monkeypatch):
    """Флаг инверсии из СЫРОГО узла каскада доходит до facts-словаря graph_build.node_to_facts — именно
    его читают _research_thesis / дело судьи / _node_brief → бот. stage-review 31.10 (блокер №1): прежний
    тест проверял строку исходника ДРУГОЙ функции (build_chain_nodes) и был зелёным при мёртвом B1 в бою.
    Здесь — реальный сквозной прогон node_to_facts → _research_thesis с заглушками движковых fn."""
    from orchestrator import graph_build as GB
    monkeypatch.setattr(GB, "_isolation_r2_and_sigma", lambda con, root, sym: (None, None))
    monkeypatch.setattr(GB, "_adv", lambda con, sym: None)
    monkeypatch.setattr(GB.U, "is_sealable", lambda sym, con=None: True)
    raw = {"узел": "FRO.US", "chokepoint": True, "инверсия": True, "amplitude": 0.03,
           "amplitude_total": 0.05, "order": 2, "lag_total": 3}
    facts = GB.node_to_facts(raw, con=None, horizon_days=5)
    assert facts["инверсия"] is True
    plain = GB.node_to_facts({**raw, "инверсия": False}, con=None, horizon_days=5)
    assert plain["инверсия"] is False
    no_key = GB.node_to_facts({k: v for k, v in raw.items() if k != "инверсия"}, con=None, horizon_days=5)
    assert no_key["инверсия"] is False
    # читатель видит именно facts: тезис суда получает пометку только через этот словарь
    assert "ИНВЕРСН" in EF._research_thesis(facts, "событие")
    assert "ИНВЕРСН" not in EF._research_thesis(plain, "событие")


def test_inversion_text_does_not_claim_winner():
    """stage-review 31.10 №4: пометка — гипотеза картографа; «выигрывает» без сверки знака не утверждаем,
    сторона — из расчёта движка."""
    t = EF._research_thesis({"amplitude": -0.05, "edge_total": -0.05, "order": 2, "инверсия": True}, "x")
    assert "выигрывает" not in t.lower() and "гипотез" in t.lower() and "из расчёта движка" in t


def test_research_thesis_marks_inversion():
    n = {"amplitude": 0.03, "edge_total": 0.05, "order": 2, "инверсия": True}
    t = EF._research_thesis(n, "нестабильная нефть")
    assert "ИНВЕРСН" in t and "толпа ждёт ОБРАТНОГО" in t


def test_research_thesis_no_inversion_line_when_absent():
    n = {"amplitude": 0.03, "edge_total": 0.05, "order": 2}
    t = EF._research_thesis(n, "обычное событие")
    assert "ИНВЕРСН" not in t
