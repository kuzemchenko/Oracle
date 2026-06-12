# -*- coding: utf-8 -*-
"""Тесты детерминированной оценки маскированных кейсов §23.2(б) (mathlib/masked_eval).

Проверяет: смешанный набор affirm/reject честно измеряет РАЗЛИЧЕНИЕ (а не настройку под один
ответ); зачёт требует одновременно корректного рассуждения + чистоты П8 + ответов §4;
gate ≥70%; пустой вход не «проходит» порог на нуле (П8)."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mathlib import masked_eval as ME  # noqa: E402


def _affirm(**kw):
    base = dict(expected_stance="affirm", rubric_mean=4.0, verdict_outcome="УСТОЯЛА",
                p8_violations=0, mandatory_answered=True, proposed_direction="лонг")
    base.update(kw)
    return ME.score_case("c", **base)


def _reject(**kw):
    base = dict(expected_stance="reject", rubric_mean=2.0, verdict_outcome="РАЗБИТА",
                p8_violations=0, mandatory_answered=True, proposed_direction="шорт")
    base.update(kw)
    return ME.score_case("c", **base)


def test_affirm_upheld_passes():
    r = _affirm()
    assert r["reasoning_correct"] and r["case_passed"]
    assert r["rubric_pct"] == 80.0


def test_affirm_broken_fails():
    # реальный каскад, но контур его РАЗБИЛ → рассуждение неверно для affirm
    r = _affirm(verdict_outcome="РАЗБИТА", rubric_mean=2.0)
    assert not r["reasoning_correct"] and not r["case_passed"]


def test_reject_broken_passes():
    # ловушка: верное рассуждение ⟹ контур НЕ сертифицирует
    assert _reject(verdict_outcome="РАЗБИТА")["case_passed"]
    assert _reject(verdict_outcome="ВЕТО")["case_passed"]


def test_reject_upheld_directional_fails():
    # НАПРАВЛЕННУЮ ставку на ловушку ошибочно сертифицировали (УСТОЯЛА+шорт) — провал (фабрикация)
    r = _reject(verdict_outcome="УСТОЯЛА", rubric_mean=4.0, proposed_direction="шорт")
    assert not r["reasoning_correct"] and not r["case_passed"]


def test_reject_refusal_passes():
    # контур ОТКАЗАЛСЯ от сделки (направление «нет»), судья подтвердил качество отказа (УСТОЯЛА) —
    # это сильнейшая форма «не сертифицировать ловушку» → зачёт (§23.2б, mc04)
    r = _reject(verdict_outcome="УСТОЯЛА", rubric_mean=4.0, proposed_direction="нет")
    assert r["reasoning_correct"] and r["case_passed"]


def test_affirm_refusal_fails():
    # на реальном каскаде контур СПАСОВАЛ (направление «нет») — пропустил идею → не зачёт
    r = _affirm(verdict_outcome="УСТОЯЛА", rubric_mean=4.0, proposed_direction="нет")
    assert not r["reasoning_correct"] and not r["case_passed"]


def test_p8_violation_blocks_pass():
    assert not _affirm(p8_violations=2)["case_passed"]


def test_mandatory_unanswered_blocks_pass():
    assert not _affirm(mandatory_answered=False)["case_passed"]


def test_invalid_stance_raises():
    try:
        ME.score_case("c", expected_stance="maybe", rubric_mean=4.0,
                      verdict_outcome="УСТОЯЛА", p8_violations=0, mandatory_answered=True)
        assert False, "ожидался ValueError"
    except ValueError:
        pass


def test_aggregate_gate_passes_when_rejects_clean_and_fraction_ok():
    # 8 affirm зачтены + 5 reject зачтены = 13/13 → gate пройден (доля ≥70% И все reject верны)
    rs = [_affirm() for _ in range(8)] + [_reject() for _ in range(5)]
    agg = ME.aggregate(rs)
    assert agg["n_кейсов"] == 13 and agg["n_зачтено"] == 13
    assert agg["reject_дисциплина_ок"] is True
    assert agg["gate_пройден"] is True


def test_fraction_ok_but_one_affirm_fails_still_passes():
    # 7 affirm pass + 1 affirm fail + 5 reject pass = 12/13 = 92% ≥70%, все reject верны → пройден
    rs = [_affirm() for _ in range(7)] + [_affirm(verdict_outcome="РАЗБИТА", rubric_mean=2.0)] + \
         [_reject() for _ in range(5)]
    agg = ME.aggregate(rs)
    assert agg["доля_ок"] is True and agg["reject_дисциплина_ок"] is True
    assert agg["gate_пройден"] is True


def test_reject_discipline_is_hard_gate():
    # доля высокая (12/13=92%≥70%), НО один reject сертифицирован как ставка → gate ПРОВАЛЕН
    rs = [_affirm() for _ in range(8)] + [_reject() for _ in range(4)] + \
         [_reject(verdict_outcome="УСТОЯЛА", rubric_mean=4.0, proposed_direction="шорт")]
    agg = ME.aggregate(rs)
    assert agg["доля_ок"] is True           # по проценту прошёл бы
    assert agg["reject_дисциплина_ок"] is False
    assert agg["gate_пройден"] is False     # но защитная дисциплина не торгуется
    assert agg["reject_провалены"]


def test_no_reject_cases_fails_gate():
    # набор без reject-кейсов: защиту не на чём проверить → gate не пройден
    agg = ME.aggregate([_affirm() for _ in range(13)])
    assert agg["n_reject"] == 0 and agg["gate_пройден"] is False


def test_aggregate_below_threshold():
    # rejects чисты, но affirm-провалов много → доля <70% → fail
    rs = [_affirm() for _ in range(2)] + [_affirm(verdict_outcome="РАЗБИТА", rubric_mean=2.0) for _ in range(4)] + \
         [_reject() for _ in range(2)]
    agg = ME.aggregate(rs)        # 4/8 = 50% < 70%
    assert agg["reject_дисциплина_ок"] is True
    assert agg["доля_ок"] is False and agg["gate_пройден"] is False


def test_empty_set_not_passing():
    # П8: нельзя «пройти» порог на нуле кейсов
    agg = ME.aggregate([])
    assert agg["n_кейсов"] == 0 and agg["gate_пройден"] is False


def test_degenerate_affirm_all_strategy_fails_rejects():
    # «подтверждай всё»: 4 affirm проходят, 2 reject сертифицированы → дисциплина нарушена → fail
    rs = [_affirm() for _ in range(4)] + \
         [_reject(verdict_outcome="УСТОЯЛА", rubric_mean=4.0, proposed_direction="лонг") for _ in range(2)]
    agg = ME.aggregate(rs)
    assert agg["reject_дисциплина_ок"] is False
    assert agg["gate_пройден"] is False


def test_masking_flag_propagates():
    assert _affirm(masking_imperfect=True)["маскировка_несовершенна"] is True
    assert ME.aggregate([_affirm()])["оговорка"]
