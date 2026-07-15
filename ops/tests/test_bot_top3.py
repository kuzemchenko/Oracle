# -*- coding: utf-8 -*-
"""A1 (согласие владельца 15.07 «А+B»): «Топ-3» по БАЛЛУ СИЛЫ исследовательского разбора.

Закрепляет разворот, который просил владелец («фильтр в принципе не пропускает ни одной идеи»):
  • идеи РАНЖИРУЮТСЯ по силе (research_subrubric), а НЕ убиваются денежным судом — «защищать нечего»
    больше НЕ появляется, пока есть хоть одна идея;
  • №1 = сильнейшая по баллу; идея, прошедшая ДЕНЕЖНЫЙ суд §11, — выше всех (редкий сильный сигнал);
  • подача: балл X/5 + разбивка по критериям + «почему может сработать» + «кто против / слабое место»;
  • провизорную честно метим «связь ценами не подтверждена» (герметичность provisional≠money);
  • idx (номер→тикер) совпадает с порядком строк; жаргон/эмодзи не проходят линтер;
  • английский заголовок без якоря не утекает; раскрытие по номеру из готового разбора.
Сети/БД нет: фикстуры — обычные dict-протоколы.
"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))

import bot_reports as R          # noqa: E402
from ops import presentation_lint as PL   # noqa: E402


def _node(asset, *, edge=0.05, rel=0.4, order=1, prov=True, event=None, anchor="BZ"):
    return {"актив": asset, "score": 0.1, "edge": edge,
            "направление": "лонг" if edge >= 0 else "шорт",
            "надёжность_r2": rel, "порядок": order, "чокпоинт": order == 1,
            "провизорный": prov, "событие": event, "якорь": anchor,
            "лаг_дней": 4, "горизонт_дней": 5, "вероятность": 0.4}


def _idea(score, *, кто=None, почему=None, крит=None, band=None, side="лонг"):
    """Вердикт исследовательского разбора (разбор_идей)."""
    b = band or ("сильная" if score >= 3.5 else "средняя" if score >= 2.5 else "слабая")
    return {"оценка": score, "band": b, "направление": side, "критерии": крит or {},
            "кто_против": кто, "почему_возможность": почему}


def _proto(*, разбор=None, суд=None, топ=None, поток=15):
    топ = топ if топ is not None else [
        _node("FRO", edge=0.08, rel=0.42, order=1, event="Атака на танкер у Ормуза — рост фрахта"),
        _node("LNG", edge=0.05, rel=0.20, order=3, event="Обход Ормуза через СПГ"),
        _node("DAL", edge=-0.03, rel=0.08, order=2, event="Дорогое топливо бьёт по авиа"),
    ]
    return {"run_id": "ef_20260715T090000Z", "ts": "2026-07-15T09:00:00+00:00",
            "поток_идей": {"всего": поток},
            "граф_отбор": {"топ_k": топ, "разбор_идей": разбор or {}, "суд_money": суд or {}}}


_STRONG_FRO = {"FRO": _idea(3.7, кто="Деэскалация вернёт ставки вниз.",
                            почему="Рынок ещё не заложил закрытие маршрута.",
                            крит={"mechanism_real": 4, "non_obviousness": 4, "crowd_fresh": 3,
                                  "timing_amplitude": 4, "asymmetry": 3, "falsifiability": 4})}


def test_clean_and_sections():
    text, meta, viol = R.top3_from_protocol(_proto(разбор=_STRONG_FRO))
    assert viol == [] and text is not None
    assert PL._emoji_glyphs(text) == []
    low = text.lower()
    assert "что это значит для тебя" in low and "что делать" in low
    assert R.RESEARCH_FRAME in text


def test_ranks_by_strength_score():
    """№1 = идея с наибольшим баллом силы; порядок строк = порядок баллов (убыв.)."""
    разбор = {"FRO": _idea(3.7), "LNG": _idea(3.1), "DAL": _idea(2.2)}
    text, meta, viol = R.top3_from_protocol(_proto(разбор=разбор))
    assert viol == [] and meta["idx"] == {"1": "FRO", "2": "LNG", "3": "DAL"}
    assert "Сила 3.7/5" in text and "сильная" in text


def test_lower_ranked_but_higher_score_leads():
    """Идея НИЖЕ по продуктовому рангу, но ВЫШЕ по баллу силы — становится №1 (ранжируем по силе)."""
    разбор = {"FRO": _idea(2.0), "LNG": _idea(2.1), "DAL": _idea(3.8)}   # DAL слабее в топ_k, но сильнее по баллу
    text, meta, viol = R.top3_from_protocol(_proto(разбор=разбор))
    assert viol == [] and meta["idx"]["1"] == "DAL"


def test_never_says_nothing_to_defend_on_weak_day():
    """Все идеи слабые → НЕ «защищать нечего»: показываем лучшую с честным «слабовата», это и есть
    ответ владельцу «фильтр не пропускает ни одной». Идея видна, не убита."""
    разбор = {"FRO": _idea(2.1), "LNG": _idea(1.8), "DAL": _idea(1.5)}
    text, meta, viol = R.top3_from_protocol(_proto(разбор=разбор))
    assert viol == []
    assert "защищать нечего" not in text.lower()
    assert "слабовата" in text.lower() and meta["idx"]["1"] == "FRO"


def test_money_survivor_leads_over_research():
    """Идея, прошедшая ДЕНЕЖНЫЙ суд §11 (money_survived), — выше любого исследовательского балла."""
    разбор = {"LNG": _idea(3.9)}
    суд = {"FRO": {"исход": "УСТОЯЛА", "балл": 3.2, "порог": 3.0, "направление": "лонг",
                   "кто_против": "риск деэскалации", "почему_возможность": "не в цене"}}
    text, meta, viol = R.top3_from_protocol(_proto(разбор=разбор, суд=суд))
    assert viol == [] and meta["idx"]["1"] == "FRO"
    assert "денежный суд" in text.lower()


def test_provisional_caveat_present_for_research_lead():
    """Провизорная идея №1 (research) метится «связь ценами пока НЕ подтверждена»."""
    text, _, viol = R.top3_from_protocol(_proto(разбор=_STRONG_FRO))
    assert viol == []
    assert "ценами пока НЕ подтверждена" in text and "денежная ставка" in text


def test_criteria_breakdown_shown():
    """Разбивка по критериям идёт в разбор №1 (механизм/неочевидность/…)."""
    text, _, _ = R.top3_from_protocol(_proto(разбор=_STRONG_FRO))
    assert "механизм 4" in text and "неочевидность 4" in text


def test_idx_matches_display_order():
    разбор = {"FRO": _idea(3.7), "LNG": _idea(3.1), "DAL": _idea(2.2)}
    text, meta, _ = R.top3_from_protocol(_proto(разбор=разбор))
    for num, tk in meta["idx"].items():
        assert f"{num}. {tk} " in text


def test_jargon_in_verdict_does_not_break_lint():
    """Служебный жаргон в реплике суда (каскад/ярус/r²/σ) чистится — линтер НЕ валит пуш."""
    dirty = {"FRO": _idea(3.6, кто="Эффект каскада затухает, ярус низкий (r²≈0.1, шум σ высок).",
                         почему="FDR не срезал; z-score высокий.")}
    text, _, viol = R.top3_from_protocol(_proto(разбор=dirty))
    assert viol == []
    assert "каскад" not in text.lower() and "ярус" not in text.lower() and "r²" not in text


def test_defense_card_by_number():
    proto = _proto(разбор=_STRONG_FRO)
    card, viol = R.top3_defense_card(proto, "FRO")
    assert viol == [] and card is not None
    assert "Сила идеи: 3.7/5" in card and PL._emoji_glyphs(card) == []
    none_card, v2 = R.top3_defense_card(proto, "ZZZ")
    assert none_card is None and v2


def test_english_headline_without_anchor_is_omitted():
    en = "Trump threatens to strike Iranian power plants next week"
    топ = [_node("XYZ", edge=0.05, rel=0.3, order=2, event=en, anchor=None)]
    text, _, viol = R.top3_from_protocol(_proto(топ=топ, разбор={"XYZ": _idea(2.5)}, поток=3))
    assert viol == [] and "Trump" not in text and "threatens" not in text


def test_english_headline_replaced_by_russian_mechanism():
    en = "Trump threatens to strike Iranian power plants - Jordan News | Latest News from Jo"
    топ = [_node("GEV", edge=0.06, rel=0.3, order=2, event=en, anchor="VRT")]
    text, _, viol = R.top3_from_protocol(_proto(топ=топ, разбор={"GEV": _idea(2.6)}, поток=5))
    assert viol == [] and "Trump" not in text and "Jordan News" not in text
    assert "импульс по" in text.lower()


def test_unjudged_is_honest_scan():
    """Разбора нет вовсе → честно «глубоко не оценивал, наводки», без выдуманных баллов."""
    text, meta, viol = R.top3_from_protocol(_proto(разбор={}, суд={}))
    assert viol == [] and meta["idx"]["1"]
    assert "наводк" in text.lower() and "без оценки" in text.lower()


def test_empty_day_is_honest_and_clean():
    text, meta, viol = R.top3_from_protocol(_proto(топ=[]))
    assert viol == [] and "пустой день" in text.lower()
    assert meta["idx"] == {}


def test_no_double_period_in_oneliner():
    text, _, _ = R.top3_from_protocol(_proto(разбор=_STRONG_FRO))
    assert ".." not in text
