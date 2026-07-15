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
    """Постмортем строится из РЕАЛЬНОЙ схемы outcomes.jsonl (asset/direction/threshold/outcome)."""
    p = _proto(top_k=[_node("GEV.US")], суд={"GEV.US": {"исход": "УСТОЯЛА"}})
    out = [{"asset": "FRO.US", "kind": "cascade_provisional", "direction": "above",
            "threshold": 36.75, "observed_value": 36.92, "outcome": 1, "probability": 0.5045,
            "resolve_by": "2026-07-13T20:00:00+00:00"}]
    case = DC.select_case(p, outcomes=out)
    assert case["статус"] == "resolved_postmortem"
    assert case["актив"] == "FRO.US"
    assert "СБЫЛСЯ" in case["заголовок"]
    баллы = dict(case["баллы"])
    assert баллы["исход"] == "прогноз СБЫЛСЯ"
    assert case["актив"] != "?"


def test_postmortem_stale_outcome_yields_to_candidate():
    """stage-review: несвежий исход (разрешён давно) НЕ даёт постмортем — иначе один и тот же
    «сегодня подводим итог» топит живого кандидата много дней. Свежий (≤2 дн) — даёт."""
    p = _proto(top_k=[_node("GEV.US")], суд={"GEV.US": {"исход": "УСТОЯЛА"}}, ts="2026-07-15T09:00:00Z")
    stale = [{"asset": "FRO.US", "direction": "above", "threshold": 36.75, "observed_value": 36.92,
              "outcome": 1, "resolved_at": "2026-07-01T21:00:00+00:00"}]     # 14 дней назад
    assert DC.select_case(p, outcomes=stale)["статус"] == "live_candidate"
    fresh = [{"asset": "FRO.US", "direction": "above", "threshold": 36.75, "observed_value": 36.92,
              "outcome": 1, "resolved_at": "2026-07-14T21:00:00+00:00"}]     # вчера
    assert DC.select_case(p, outcomes=fresh)["статус"] == "resolved_postmortem"


def test_postmortem_skipped_when_outcome_lacks_asset():
    """Исход без актива/факта НЕ даёт постмортем (нечего сверять) — кейс берётся из кандидатов."""
    p = _proto(top_k=[_node("GEV.US")], суд={"GEV.US": {"исход": "УСТОЯЛА"}})
    case = DC.select_case(p, outcomes=[{"kind": "x"}, {"asset": None}])
    assert case["статус"] == "live_candidate"


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


def test_skip_verdict_not_labeled_as_passed_court():
    """stage-review #7 (П8, анти-инфляция): ПРОПУСК (узел исключён ДО суда, нет данных) НЕ выдаётся
    за «прошёл состязательный суд». Статус live_candidate, но человеко-метка честная."""
    p = _proto(top_k=[_node("GEV.US")], суд={"GEV.US": {"исход": "ПРОПУСК", "примечание": "нет котировок"}})
    case = DC.select_case(p)
    assert case["статус"] == "live_candidate"
    assert case["судебное_состояние"] == "skip"
    assert "не смог" in case["статус_человек"].lower() or "не вынес" in case["статус_человек"].lower()
    assert "прош" not in case["статус_человек"].lower()          # НЕ «прошёл суд»
    assert "не смог вынести" in case["статус_воронки"].lower() or "нет" in case["статус_воронки"].lower()


def test_untried_node_not_labeled_as_passed_court():
    """court=None (суд не гонялся, напр. под --vet) → «ещё НЕ прошла суд», не «дошёл до суда сегодня»."""
    p = _proto(top_k=[_node("ADM.US")], суд={})                  # нет вердикта
    case = DC.select_case(p)
    assert case["статус"] == "live_candidate" and case["судебное_состояние"] == "untried"
    assert "не прош" in case["статус_человек"].lower() or "сыр" in case["статус_человек"].lower()
    assert "прошёл слепой" not in case["значит_для_тебя"].lower()


def test_passed_court_labeled_honestly():
    p = _proto(top_k=[_node("X.US")], суд={"X.US": {"исход": "УСТОЯЛА"}})
    case = DC.select_case(p)
    assert case["судебное_состояние"] == "passed"
    assert "прош" in case["статус_человек"].lower()


def test_headline_no_unbacked_priced_claim():
    """stage-review #5: «ещё не отыграл» утверждаем ТОЛЬКО при измеренной низкой отыгранности."""
    p_nodata = _proto(top_k=[_node("X.US")], суд={})             # без отыгранность_узла
    assert "не отыграл" not in DC.select_case(p_nodata)["заголовок"]
    p_low = _proto(top_k=[_node("Y.US", отыгранность_узла=0.15)], суд={})
    assert "не отыграл" in DC.select_case(p_low)["заголовок"]


def test_status_label_matches_reality():
    """статус_человек соответствует фактическому статусу (не переклеиваем ярлык)."""
    p = _proto(top_k=[_node("X.US")], суд={"X.US": {"исход": "РАЗБИТА"}})
    case = DC.select_case(p)
    assert "Вскрытие" in case["статус_человек"]
