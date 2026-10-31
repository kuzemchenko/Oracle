# -*- coding: utf-8 -*-
"""A1: выбор ИССЛЕДОВАТЕЛЬСКОЙ под-рубрики судьи (research_subrubric) и её отделённость от денежной.

Чистые функции выбора рубрики (без LLM/сети):
  • вид_дела=='исследование' → research_subrubric (сила идеи; r² не ворота);
  • дело каскадное без вида → cascade_subrubric (деньги, требует исторический edge);
  • research_subrubric НЕ содержит transfer_reliability/edge_over_realized как критериев-ворот;
  • research приоритетнее cascade, когда есть и дело, и вид='исследование'.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import debate as D          # noqa: E402


def _ids(criteria):
    return {c["id"] for c in criteria}


def test_research_view_selects_research_subrubric():
    rub = D.load_rubric()
    дело = {"событие_исток_заголовок": "x"}
    crit = D._active_criteria(rub, дело, "исследование")
    ids = _ids(crit)
    assert "non_obviousness" in ids and "mechanism_real" in ids
    # research НЕ судит по историческому edge как воротам (в этом весь смысл A1)
    assert "transfer_reliability" not in ids and "edge_over_realized" not in ids


def test_cascade_view_unchanged_for_money():
    """Денежный путь (дело без вида) по-прежнему берёт cascade_subrubric с transfer_reliability."""
    rub = D.load_rubric()
    дело = {"событие_исток_заголовок": "x"}
    ids = _ids(D._active_criteria(rub, дело, None))
    assert "transfer_reliability" in ids and "edge_over_realized" in ids


def test_base_rubric_without_case():
    rub = D.load_rubric()
    ids = _ids(D._active_criteria(rub, None, None))
    # базовые 6 критериев (не каскад/не research)
    assert ids and "non_obviousness" not in ids and "transfer_reliability" not in ids


def test_research_priority_over_cascade():
    """При наличии и каскадного дела, и вида='исследование' — судим по research (не деньги)."""
    rub = D.load_rubric()
    дело = {"событие_исток_заголовок": "x"}
    ids = _ids(D._active_criteria(rub, дело, "исследование"))
    assert "non_obviousness" in ids and "transfer_reliability" not in ids


def test_research_mandatory_questions_present():
    rub = D.load_rubric()
    mq = D._active_mandatory(rub, {"x": 1}, "исследование")
    assert isinstance(mq, list) and len(mq) == 2
