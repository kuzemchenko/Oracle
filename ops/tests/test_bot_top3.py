# -*- coding: utf-8 -*-
"""Flow-fix В1 (решение владельца 15.07): «Топ-3 одной строкой» + защита №1.

Закрепляет продукт, который владелец выбрал взамен «Разбора дня»:
  • format_top3 / top3_from_protocol — три сильнейшие идеи по строке, №1 с защитой; стиль-контракт
    (без эмодзи/жаргона, обязательные секции) держится fail-closed;
  • №1 = идея, реально прошедшая состязательный суд (защита_гипотез), а не просто топ ранга —
    в этом весь смысл Вариант 1 (владельцу приходит ЗАЩИЩЁННАЯ идея, а не сырая наводка);
  • порядок idx (номер→тикер) совпадает с показанным порядком строк;
  • жаргон в свободном тексте суда (каскад/ярус/r²) НЕ роняет линтер — чистится _clean_llm;
  • top3_defense_card — раскрытие по номеру из ГОТОВОГО суда, без нового прогона;
  • пустой день — честная строка, проходит контракт.
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


def _proto(*, защита=None, суд=None, топ=None, поток=10):
    топ = топ if топ is not None else [
        _node("FRO", edge=0.08, rel=0.42, order=1, event="Атака на танкер у Ормуза — рост фрахта"),
        _node("LNG", edge=0.05, rel=0.20, order=3, event="Обход Ормуза через СПГ"),
        _node("DAL", edge=-0.03, rel=0.08, order=2, event="Дорогое топливо бьёт по авиа"),
    ]
    return {"run_id": "ef_20260715T090000Z", "ts": "2026-07-15T09:00:00+00:00",
            "поток_идей": {"всего": поток},
            "граф_отбор": {"топ_k": топ, "защита_гипотез": защита or {}, "суд_money": суд or {}}}


_FRO_VERDICT = {"FRO": {"исход": "УСТОЯЛА", "направление": "лонг", "балл": 3.4, "порог": 3.0,
                        "кто_против": "Флот в Заливе избыточен, деэскалация вернёт ставки вниз.",
                        "почему_возможность": "Рынок ещё не заложил закрытие маршрута; ставки лагают."}}


def test_top3_clean_and_sections():
    """Базовый: текст проходит стиль-контракт (0 нарушений), есть обе обязательные секции, нет эмодзи."""
    text, meta, viol = R.top3_from_protocol(_proto(защита=_FRO_VERDICT))
    assert viol == [] and text is not None
    assert PL._emoji_glyphs(text) == []
    low = text.lower()
    assert "что это значит для тебя" in low and "что делать" in low
    assert R.RESEARCH_FRAME in text


def test_lead_is_the_defended_idea():
    """№1 = идея с РЕАЛЬНОЙ защитой суда, даже если по продуктовому рангу выше другая (смысл В1)."""
    text, meta, _ = R.top3_from_protocol(_proto(защита=_FRO_VERDICT))
    assert meta["idx"]["1"] == "FRO"
    # первая строка-идея — про FRO; аргумент суда «почему возможность» присутствует в защите №1
    assert "1. " in text and "Рынок ещё не заложил закрытие маршрута" in text
    assert "Защищаю №1" in text


def test_idx_matches_display_order():
    """Номер→тикер совпадает с порядком показанных строк (иначе «скажи 2» раскроет не ту идею)."""
    text, meta, _ = R.top3_from_protocol(_proto(защита=_FRO_VERDICT))
    # находим номера строк-идей в тексте по «N. ТИКЕР»
    for num, tk in meta["idx"].items():
        assert f"{num}. {tk} " in text


def test_jargon_in_court_text_does_not_break_lint():
    """Служебный жаргон в реплике суда (каскад/ярус/r²/σ) чистится — линтер НЕ валит пуш."""
    dirty = {"FRO": {"исход": "УСТОЯЛА", "балл": 3.2, "порог": 3.0,
                     "кто_против": "Эффект каскада затухает, ярус надёжности низкий (r²≈0.1, шум σ высок).",
                     "почему_возможность": "FDR не срезал сигнал; z-score высокий."}}
    text, meta, viol = R.top3_from_protocol(_proto(защита=dirty))
    assert viol == [] and text is not None
    assert "каскад" not in text.lower() and "ярус" not in text.lower() and "r²" not in text


def test_defense_card_by_number():
    """Раскрытие по номеру берёт готовый вердикт суда и проходит контракт (без нового прогона)."""
    proto = _proto(защита=_FRO_VERDICT)
    card, viol = R.top3_defense_card(proto, "FRO")
    assert viol == [] and card is not None
    assert "устояла под критикой" in card and PL._emoji_glyphs(card) == []
    # неизвестный тикер → честный отказ, не выдумка
    none_card, v2 = R.top3_defense_card(proto, "ZZZ")
    assert none_card is None and v2


def test_survived_lead_is_defended():
    """Лид ПРОШЁЛ суд (УСТОЯЛА) → подача «Защищаю №1», секция «почему возможность» из суда."""
    text, meta, viol = R.top3_from_protocol(_proto(защита=_FRO_VERDICT))
    assert viol == [] and meta["idx"]["1"] == "FRO"
    assert "Защищаю №1" in text and "прошла мой независимый суд" in text
    assert "Рынок ещё не заложил закрытие маршрута" in text     # судейское «за» выжившей


def test_all_rejected_is_honest_not_faked_defense():
    """ВСЕ идеи разбиты судом → НЕ выдаём разбитую за защищённую: прямо «защищать не буду»,
    показываем как ближайшую к порогу и главное возражение суда. Это ответ владельцу «ты сам их
    убиваешь»: честно, где не хватило — а не пустой питч мусора."""
    rejected = {"FRO": {"исход": "РАЗБИТА", "направление": "лонг", "балл": 1.3, "порог": 3.0,
                        "кто_против": "Флот избыточен, деэскалация вернёт ставки — они, скорее, правы.",
                        "почему_возможность": "Возможность крайне сомнительна: нет расчёта реализованной части."}}
    text, meta, viol = R.top3_from_protocol(_proto(защита=rejected))
    assert viol == []
    assert "защищать не буду" in text.lower() or "защищать нечего" in text.lower()
    assert "Защищаю №1" not in text                       # разбитую НЕ выдаём за защищённую
    assert "разбита критикой" in text and "1.3 из 3" in text


def test_no_court_is_honest_scan_not_verdict():
    """Нет суда вовсе — лид по рангу, честная пометка «через суд не гоняли», без слова «защищаю»."""
    text, meta, viol = R.top3_from_protocol(_proto(защита={}, суд={}))
    assert viol == [] and text is not None and meta["idx"]["1"]
    assert "Защищаю №1" not in text
    assert "через" in text.lower() and "суд" in text.lower()


def test_empty_day_is_honest_and_clean():
    """Пустой день (нет узлов) — честная строка, проходит стиль-контракт (не отписка)."""
    text, meta, viol = R.top3_from_protocol(_proto(топ=[]))
    assert viol == [] and "пустой день" in text.lower()
    assert meta["idx"] == {}


def test_no_double_period_in_oneliner():
    """Склейка повода и риска не плодит «..» (повод/риск с финальной точкой обрезаются)."""
    text, _, _ = R.top3_from_protocol(_proto(защита=_FRO_VERDICT))
    assert ".." not in text


def test_english_headline_replaced_by_russian_mechanism():
    """Английский заголовок-повод НЕ уходит владельцу: строка показывает русский механизм цепочки."""
    en = "Trump threatens to strike Iranian power plants next week - Jordan News | Latest News from Jo"
    топ = [_node("GEV", edge=0.06, rel=0.3, order=2, event=en, anchor="VRT")]
    text, _, viol = R.top3_from_protocol(_proto(топ=топ, поток=5))
    assert viol == []
    assert "Trump" not in text and "Jordan News" not in text and "from Jo" not in text
    assert "импульс по" in text.lower()               # русский механизм-фолбэк


def test_russian_event_keeps_content_strips_source():
    """Русский повод сохраняется, но служебный хвост-источник (« - Reuters») срезается."""
    ru = "Иран угрожает энергоблокам региона, растёт риск перебоев поставок - Reuters"
    txt, is_en = R._clean_event(ru)
    assert is_en is False and "Reuters" not in txt and "Иран угрожает" in txt


def test_word_boundary_truncation_no_mid_word_cut():
    """Обрезка длинного повода идёт по границе слова (не обрывает слово посередине)."""
    long_ru = "Правительство объявило о новых ограничениях экспорта редкоземельных металлов " * 3
    out = R._trunc_words(long_ru, 60)
    assert out.endswith("…") and not out[:-1].endswith(" ")
    assert " " in out and not out[:-1].split()[-1].endswith("-")
