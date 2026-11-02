# -*- coding: utf-8 -*-
"""Тесты V6 «прецедент механизма в пуш» (ops/mechanism_match.py + вставка в Идею дня).

Закрепляют: стеммер ловит русскую морфологию (газа=газ, поставок=поставки); матч по
боевой библиотеке попадает в правдоподобный механизм на 4 реалистичных текстах;
мусор → честный None; строка «Похожий случай» держит все числа внутри «…» (линт
≤4 чисел цел); подача с матчем проходит линт, без матча — живёт без строки."""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT / "ops" / "tests"))

import mechanism_match as MM          # noqa: E402
import bot_reports as R               # noqa: E402
import presentation_lint as PL        # noqa: E402

from test_idea_of_day import _entry, _proto, KW, COURT    # noqa: E402

IDX = MM.load_index()


def test_stemmer_unifies_russian_forms():
    assert MM._norm("газа") == MM._norm("газ")
    assert MM._norm("поставок") == MM._norm("поставки")
    assert MM._norm("танкерный") == MM._norm("танкерные")


def test_match_plausible_on_battle_library():
    cases = {
        "перебои поставок газа в Европу, танкерный фрахт дорожает, экспортёр СПГ выигрывает":
            {"sanctions_flow_rerouting", "chokepoint_disruption", "fuel_substitution_demand_shock",
             "gas_ammonia_cascade", "energy_supply_shock"},
        "дата-центры под ИИ требуют электроэнергии, сделки гиперскейлеров с атомными станциями":
            {"datacenter_power_demand", "hyperscaler_capex_cascade"},
        "ураган остановил нефтепереработку, крэк-спреды растут, бензин дорожает":
            {"refinery_capacity_shock"},
    }
    for text, ok_ids in cases.items():
        m = MM.match(text, IDX)
        assert m is not None, text
        assert m[0]["id"] in ok_ids, (text, m[0]["id"])
    assert MM.match("погода хорошая, настроение отличное, кошка спит", IDX) is None


def test_similar_case_line_keeps_digits_quoted():
    m = MM.match("перебои поставок газа, танкерный фрахт, экспортёр СПГ", IDX)
    line = MM.similar_case_line(m[0])
    assert line.startswith("Похожий случай")
    assert "«" in line and "»" in line
    bare = re.sub(r"«[^»]*»", "", line)       # вне кавычек чисел быть не должно
    assert not re.search(r"\d", bare), bare


def test_idea_of_day_carries_similar_case_and_passes_lint():
    node = ("перебои поставок газа в Европу толкают цены СПГ вверх, танкерный фрахт дорожает, "
            "экспортёр СПГ из США выигрывает на узком горле поставок")
    top = [_entry("LNG.US", 2, 0.2, node=node)]
    court = {"LNG.US": COURT["NVDA.US"]}
    text = R.format_idea_of_day(_proto(top, court=court), **KW)
    assert "Похожий случай" in text
    assert PL.lint_idea_of_day(text) == [], PL.lint_idea_of_day(text)


def test_idea_of_day_without_match_has_no_line():
    top = [_entry("NVDA.US", 2, 0.2, node="узел цепочки")]
    text = R.format_idea_of_day(_proto(top), **KW)
    assert "Похожий случай" not in text      # слабый матч → строки честно нет
    assert PL.lint_idea_of_day(text) == [], PL.lint_idea_of_day(text)


def test_obs_sign_reads_numeric_notation():
    """Ре-гейт №2 02.11: знак читается и из числовой нотации библиотеки (75% прецедентов),
    словесные маркеры — префиксом токена, «кратно» больше не тянет вверх."""
    assert MM._obs_sign("фрахт +164% за 3 недели") == "вверх"
    assert MM._obs_sign("цены −~40% от максимума") == "вниз"
    assert MM._obs_sign("нефть ~$3 → ~$12/б ступенями") == "вверх"
    assert MM._obs_sign("спред $12 → $4") == "вниз"
    assert MM._obs_sign("диапазон 3-5% без направления") is None
    assert MM._obs_sign("упал кратно, с 10 до 2") == "вниз"
    assert MM._obs_sign("всё просто: котировки около 100") is None   # «рост» не внутри «просто»


def test_sign_guard_fail_closed():
    """При известном направлении печатается ТОЛЬКО подтверждённый согласованный прецедент:
    противонаправленный → None; знак не распарсен → None (не подкладываем неизвестное)."""
    up_mech = {"название": "м", "counterexamples": "", "precedents": [
        {"year": 1973, "event": "эмбарго", "observed": "нефть $3 → $12 за месяцы"}]}
    assert MM.similar_case_line(up_mech, direction="вниз") is None
    assert "1973" in MM.similar_case_line(up_mech, direction="вверх")
    vague = {"название": "м", "counterexamples": "", "precedents": [
        {"year": 2020, "event": "э", "observed": "хаос на 7 рынках — знак неясен"}]}
    assert MM.similar_case_line(vague, direction="вниз") is None
    assert MM.similar_case_line(vague) is not None        # без направления — печатаем как раньше


def test_jargon_stoplist_first_use_and_quotes():
    """Испытатель №1: стоп-лист жаргона — «чокпоинт» без расшифровки = брак; с расшифровкой
    в той же фразе — чисто; внутри цитаты «…» (чужой текст) — не требуем."""
    bad = PL.lint_for_user("Что это значит для тебя. Чокпоинт перегружен.\nЧто делать. Ничего.")
    assert any("чокпоинт" in v for v in bad)
    ok = PL.lint_for_user("Что это значит для тебя. Чокпоинт (узкое горло поставок) перегружен."
                          "\nЧто делать. Ничего.")
    assert not any("жаргон" in v for v in ok)
    quoted = PL.lint_for_user("Что это значит для тебя. Суд сказал: «edge не подтверждён»."
                              "\nЧто делать. Ничего.")
    assert not any("жаргон" in v for v in quoted)
