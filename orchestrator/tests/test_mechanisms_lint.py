# -*- coding: utf-8 -*-
"""Тесты V6 — валидатор библиотеки механизмов (orchestrator/mechanisms.py).

Закрепляют: разбор лагов, зону охоты 3–13 нед, ловлю битых прецедентов (год/URL/число),
П16-чистоту форвард-счёта, фальсифицируемость («НЕ СРАБАТЫВАЕТ»/«ЗАТУХАЕТ»),
и регрессию: БОЕВАЯ knowledge/mechanisms.yaml проходит без ошибок.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import mechanisms as M     # noqa: E402


def _good(**over):
    m = {
        "id": "test_mechanism_ok", "rule": "шок → цена ↑", "theory": "эластичность",
        "trigger_class": "шок предложения",
        "chain": ["1) выпадение → цена ↑", "2) маржа потребителей ↓"],
        "conditions": ["СРАБАТЫВАЕТ: запасы низкие", "НЕ СРАБАТЫВАЕТ: быстрый ремонт"],
        "lag_weeks": "1-й ход 0–4; 2-й ход 2–12",
        "precedents": [
            {"year": "2022", "event": "эмбарго", "observed": "цена +40% за 6 недель",
             "source": "https://example.org/a"},
            {"year": "2019", "event": "авария", "observed": "откат за две недели — нюанс",
             "source": "https://example.org/b"},
        ],
        "watch_assets": "фьючерс X, сектор Y", "tradeable": True,
        "amplitude": "+20–60%", "counterexamples": "2019: быстрый ремонт — отката не было",
        "forward_score": {"вопросов": 0, "исходов": 0, "попаданий": 0},
    }
    m.update(over)
    return m


def test_lag_ranges_and_hunt_zone():
    assert M.lag_ranges("1-й ход 0–4 (пик 2–8); 2-й ход 2–12") == [(0, 4), (2, 8), (2, 12)]
    assert M.lag_ranges("около 6 недель") == [(6, 6)]
    assert M.lag_ranges("") == []
    assert M.in_hunt_zone("2–12") is True
    assert M.in_hunt_zone("0–1") is False          # быстрее зоны
    assert M.in_hunt_zone("26–100") is False       # медленнее зоны


def test_good_mechanism_passes_clean():
    rep = M.validate([_good()])
    assert rep["ошибки"] == [] and rep["в_бой"] == 1
    # качественный прецедент рядом с числовым — лишь предупреждение
    assert any("качественное" in w for w in rep["предупреждения"])


def test_broken_precedents_and_ids_are_errors():
    bad = _good(id="Bad-ID!", precedents=[
        {"year": "когда-то", "event": "", "observed": "выросло сильно",
         "source": "газета «Правда»"}])
    rep = M.validate([bad, _good(), _good()])      # дубль id test_mechanism_ok
    errs = " | ".join(rep["ошибки"])
    assert "snake_case" in errs and "дубль id" in errs
    assert "нет распознаваемого года" in errs and "пустое событие" in errs
    assert "ни одного URL" in errs
    assert "ни одного прецедента с числовой амплитудой" in errs


def test_p16_forward_score_guard():
    rep = M.validate([_good(forward_score={"вопросов": 5, "исходов": 3, "попаданий": 2})])
    assert rep["ошибки"] == []
    assert any("П16" in w for w in rep["предупреждения"])
    rep2 = M.validate([_good(forward_score={"вопросов": 1, "исходов": 2, "попаданий": 3})])
    assert any("несогласован" in e for e in rep2["ошибки"])


def test_falsifiability_markers():
    rep = M.validate([_good(conditions=["СРАБАТЫВАЕТ: всегда"])])
    assert any("нефальсифицируемо" in w for w in rep["предупреждения"])
    rep2 = M.validate([_good(conditions=["ЗАТУХАЕТ: обходные схемы обкатываются"])])
    assert not any("нефальсифицируемо" in w for w in rep2["предупреждения"])


def test_battle_library_has_no_errors():
    rep = M.validate(M.load())
    assert rep["ошибки"] == [], rep["ошибки"]
    assert rep["механизмов"] >= 51 and rep["в_бой"] == rep["механизмов"]
    assert rep["сводка"]["в_зоне_3_13_нед"] == rep["механизмов"]
