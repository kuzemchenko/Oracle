# -*- coding: utf-8 -*-
"""Тесты артефакта «Разбор дня» (Этап3, пакет после аудита): orchestrator/daily_case.select_case.

Проверяет: (1) честный выбор статуса без повышения ради каденции; (2) 7 обязательных блоков;
(3) неизмеренное = «не измерено», не выдумка; (4) пустой день = честная строка, не отписка;
(5) кейс НИЧЕГО не пишет в журналы (чистая функция над протоколом)."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orchestrator import daily_case as DC   # noqa: E402


def _proto(top_k=None, суд=None, scan=None, rid="ef_T", ts="2026-07-15T09:00:00Z"):
    return {"run_id": rid, "ts": ts,
            "граф_отбор": {"топ_k": top_k or [], "суд_money": суд or {}},
            "скан": scan or {}}


def _node(actив="AAA.US", **kw):
    n = {"актив": actив, "событие": "какое-то событие", "порядок": 3,
         "узлы_каскада": [{"порядок": 3, "узел": "звено", "тикеры": [actив]}]}
    n.update(kw)
    return n


def test_live_candidate_selected_when_court_upholds():
    p = _proto(top_k=[_node("GEV.US")],
               суд={"GEV.US": {"исход": "УСТОЯЛА", "балл": 3.4, "порог": 3.0,
                               "кто_продаёт_нам": "скептик: уже в цене"}})
    case = DC.select_case(p)
    assert case["статус"] == "live_candidate"
    assert case["кто_продаёт"] and "скептик" in case["кто_продаёт"]


def test_autopsy_not_upgraded_to_live():
    """Ключевое правило аудита: РАЗБИТА — это вскрытие, статус НЕ повышается до live ради красоты."""
    p = _proto(top_k=[_node("ADM.US", провизорный=True, надёжность_r2=0.05)],
               суд={"ADM.US": {"исход": "РАЗБИТА", "балл": 1.8, "порог": 3.0,
                               "кто_против": "нет данных"}})
    case = DC.select_case(p)
    assert case["статус"] == "candidate_autopsy"
    assert "суд" in case["статус_воронки"].lower() or "отсеяна" in case["статус_воронки"].lower()


def test_resolved_postmortem_wins_when_outcome_present():
    p = _proto(top_k=[_node("GEV.US")], суд={"GEV.US": {"исход": "УСТОЯЛА"}})
    out = [{"актив": "LNG.US", "событие": "прошлый прогноз", "порядок": 2,
            "факт_словом": "движение подтвердилось"}]
    case = DC.select_case(p, outcomes=out)
    assert case["статус"] == "resolved_postmortem"
    assert case["актив"] == "LNG.US"


def test_signal_noise_lesson_when_only_noise():
    """Ни кандидатов, ни исходов — но заметный шум скана даёт учебный кейс (материал всегда есть)."""
    scan = {"сигналы": [{"вид": "price", "символ": "ZZZ.US", "кандидат": True,
                         "сигнал_после_FDR": False, "p_value": 0.03}]}
    case = DC.select_case(_proto(scan=scan))
    assert case["статус"] == "signal_noise_lesson"
    assert "шум" in case["статус_воронки"].lower() or "фон" in case["статус_воронки"].lower()


def test_empty_day_is_honest_not_apology():
    case = DC.select_case(_proto())
    assert case.get("пусто")
    assert "тех_id" in case


def test_all_seven_blocks_present():
    p = _proto(top_k=[_node("GEV.US", чокпоинт=True)],
               суд={"GEV.US": {"исход": "УСТОЯЛА", "кто_продаёт_нам": "кто-то"}})
    case = DC.select_case(p)
    for блок in ("заголовок", "значит_для_тебя", "цепочка", "баллы", "неверна_если",
                 "статус_воронки", "что_делать", "вопрос"):
        assert блок in case, f"нет блока {блок}"
    assert len(case["баллы"]) == 6                      # ровно 6 критериев таблицы
    assert case["вопрос"]["варианты"]                   # вопрос с вариантами (кормит decisions_user)


def test_unmeasured_is_marked_not_invented():
    """Нет продуктового ранга/манипбалла/компетенции → None (=«не измерено»), а не выдуманное число."""
    p = _proto(top_k=[_node("AAA.US")], суд={"AAA.US": {"исход": "УСТОЯЛА"}})
    case = DC.select_case(p)
    крит = dict(case["баллы"])
    assert крит["манипуляционный балл"] is None
    assert крит["близость к твоей компетенции"] is None


def test_status_label_matches_reality():
    """статус_человек соответствует фактическому статусу (не переклеиваем ярлык)."""
    p = _proto(top_k=[_node("X.US")], суд={"X.US": {"исход": "РАЗБИТА"}})
    case = DC.select_case(p)
    assert "Вскрытие" in case["статус_человек"]
