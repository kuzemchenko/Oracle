# -*- coding: utf-8 -*-
"""A1 (ревью stage-review wf_1bbe7c6a, находка «д»): _criteria_scores — разбор баллов судьи в
{id_критерия: балл} для строки «сила идеи» в подаче. Ключевое требование честности (П8): позиционный
фолбэк допустим ТОЛЬКО для чисто безымянного списка ровной длины; при частичном совпадении по id
позиционно НЕ добираем (иначе подпишем балл не тем критерием). Функция питает только ОТОБРАЖЕНИЕ
критериев — не score/band/ранжир/гейт.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import event_first as EF          # noqa: E402

IDS = ["mechanism_real", "non_obviousness", "crowd_fresh",
       "timing_amplitude", "asymmetry", "falsifiability"]


def _judge(оценки):
    return {"рубрика": {"оценки": оценки}}


def test_match_by_id():
    j = _judge([{"id": "non_obviousness", "балл": 4}, {"id": "mechanism_real", "балл": 3}])
    assert EF._criteria_scores(j, IDS) == {"non_obviousness": 4.0, "mechanism_real": 3.0}


def test_bare_list_exact_length_positional():
    """Безымянный список ровно нужной длины → маппинг по позиции (порядок критериев фиксирован)."""
    j = _judge([{"балл": b} for b in (4, 3, 2, 5, 3, 4)])
    assert EF._criteria_scores(j, IDS) == dict(zip(IDS, [4.0, 3.0, 2.0, 5.0, 3.0, 4.0]))


def test_partial_id_match_does_not_positionally_fill():
    """Часть баллов с id, часть без → возвращаем ТОЛЬКО совпавшие по id; лишние по позиции НЕ добираем
    (риск подписать не тем критерием). Лучше показать меньше критериев, чем мислейбл."""
    j = _judge([{"id": "mechanism_real", "балл": 4}, {"балл": 2}, {"балл": 5}])
    assert EF._criteria_scores(j, IDS) == {"mechanism_real": 4.0}


def test_bare_list_wrong_length_returns_empty():
    """Безымянный список НЕ той длины → пусто (без позиционных догадок)."""
    j = _judge([{"балл": 4}, {"балл": 3}])
    assert EF._criteria_scores(j, IDS) == {}


def test_bool_and_nonnumeric_ignored():
    j = _judge([{"id": "mechanism_real", "балл": True}, {"id": "asymmetry", "балл": "x"},
                {"id": "crowd_fresh", "балл": 3}])
    assert EF._criteria_scores(j, IDS) == {"crowd_fresh": 3.0}


def test_no_scores_list():
    assert EF._criteria_scores({"рубрика": {}}, IDS) == {}
    assert EF._criteria_scores({}, IDS) == {}
    assert EF._criteria_scores(None, IDS) == {}
