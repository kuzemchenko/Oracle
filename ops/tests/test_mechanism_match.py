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


def test_obs_sign_battle_snapshot_not_inverted():
    """Перепроверка №2 02.11 (блокер): пробельные тысячи, даты в скобках, нечисловые
    стрелки и рассогласованные единицы давали УВЕРЕННО перевёрнутый знак."""
    assert MM._obs_sign("магний ~$2000/т → $10 000–14 000/т (5–7x) за ~2 месяца") == "вверх"
    assert MM._obs_sign(
        "Rheinmetall €96.8 (23.02.2022) → ~€968 к февралю 2025 — ~10x за 3 года") == "вверх"
    assert MM._obs_sign(
        "сахар — максимум почти за 13 лет (FAO); лаг объявление→пик ~3–4 месяца") is None
    assert MM._obs_sign("никель $12 000→$17 тыс.") is None     # единицы врозь — не сравниваем
    assert MM._obs_sign(
        "спот Китай→USWC $15 551 (18.04) → $9177 (16.06), −41% за 2 месяца") == "вниз"


def test_obs_sign_library_no_multiplier_inversion():
    """Регрессионная сеть по ЖИВОЙ библиотеке: прецедент с множителем роста (Nx / в N раз)
    без слов падения не может классифицироваться «вниз» — класс перевёрнутых знаков."""
    for m in IDX:
        for p in m["precedents"]:
            obs = str(p.get("observed") or "")
            if not re.search(r"\d", obs):
                continue
            s = MM._obs_sign(obs)
            assert s in (None, "вверх", "вниз")
            low = obs.lower()
            if (s == "вниз" and re.search(r"\d\s*[x×]|в\s+\d+\s+раз", low)
                    and not re.search(r"упал|рухнул|обвал|минус|−|сниж|вниз", low)):
                raise AssertionError(f"перевёрнутый знак: {obs[:90]}")


def test_obs_sign_comma_thousands_and_negation():
    """Перепроверка №4: запятая НЕОДНОЗНАЧНА (англ. тысячи «1,500» vs рус. десятичная
    «0,875») — знак утверждается только при согласии всех прочтений, иначе честный None
    (глобальная склейка №3 давала уверенный переворот на русской десятичной);
    «не»/«ни» перед словом снимает маркер, граница предложения рвёт связь."""
    assert MM._obs_sign("цена $1,500 → $900") is None          # 1.5 vs 1500 — прочтения спорят
    assert MM._obs_sign("курс 1,25 → 0,875") is None           # рус. десятичная с 3 знаками
    assert MM._obs_sign("маржа 0,87 → 1,25") == "вверх"        # однозначные десятичные
    assert MM._obs_sign("ставки не выросли — спрос уже падал") == "вниз"
    assert MM._obs_sign("роста нет. упали цены") == "вниз"     # «нет» не гасит чужую клаузу
    assert MM._obs_sign("роста нет") is None                   # постфиксное «нет» гасит маркер
    # живой прецедент manufacturing_hub_lockdown: «+30–40%» (заторы) и «НЕ выросли» →
    # смешанный → None, довод «за» не печатается ни под какое направление
    live = ("очередь судов сотни единиц; заторы порта +30–40%; груз ушёл в Нинбо/Циндао; "
            "Tesla Shanghai простаивала; НО глобальные ставки НЕ выросли — спрос уже падал")
    assert MM._obs_sign(live) is None


def test_lead_sign_guard_prefix_stems_and_negation():
    """Перепроверка №3 (регрессия): приставочные формы возвращены («подешевела», «прирост»,
    «выросла»), отрицание снимает маркер; смешанный текст — не конфликт (строгое правило)."""
    assert R._node_sign_conflict("сталь подешевела на внутреннем рынке", "вверх") is True
    assert R._node_sign_conflict("подешевела, но прирост экспорта компенсирует", "вверх") is False
    assert R._node_sign_conflict("выросла маржа переработчиков", "вниз") is True
    assert R._node_sign_conflict("маржа не выросла при этом", "вниз") is False
    assert R._node_sign_conflict("механизм просто работает дальше", "вниз") is False


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
    # Перепроверка №4: «Лонг-Бич» — имя, не позиция; голое «в лонг» — жаргон
    ok2 = PL.lint_for_user("Что это значит для тебя. Порт Лонг-Бич перегружен."
                           "\nЧто делать. Ничего.")
    assert not any("лонг" in v for v in ok2), ok2
    bad2 = PL.lint_for_user("Что это значит для тебя. Встаём в лонг."
                            "\nЧто делать. Ничего.")
    assert any("лонг" in v for v in bad2)
