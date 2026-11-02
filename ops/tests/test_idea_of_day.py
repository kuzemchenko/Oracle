# -*- coding: utf-8 -*-
"""V0 «Идея дня» (карта v3, 31.10.2026; формат владельца 15.07): шаблон + блокирующий линт подачи +
гард отбора лида по отыгранности (stage-review V0, блокер №1) + ветка денежного лида (регрессия №2) +
чистка чужого текста (регрессия №3). Детерминированно, без LLM/сети; базы треков подаются явно."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import bot_reports as R          # noqa: E402
import presentation_lint as PL   # noqa: E402

BASE = {"hit": 0.483, "n": 1932}
MBASE = {"hit": 0.5, "n": 24}
KW = dict(base_rate=BASE, money_base=MBASE, money_recent=0)


def _entry(asset, order, frac, edge=0.071, full=None, chain="c1", node="узел цепочки", direction="лонг"):
    # итерация-2 испытателя: дефолт полный_ход=None — как в боевых протоколах до фикса event_first:1117
    return {"актив": asset, "score": 0.3, "edge": edge, "полный_ход": full,
            "направление": direction, "ярусы": ["C"], "лаг_дней": 0, "горизонт_дней": 5,
            "вероятность": 0.467, "надёжность_r2": 0.02, "цепочка": chain, "порядок": order,
            "чокпоинт": False, "отыгранность_узла": frac,
            "узлы_каскада": [{"порядок": order, "узел": node, "тикеры": [asset]}],
            "провизорный": True, "инверсия": False, "внимание": None,
            "событие": "Технологические акции США теряют $2 трлн капитализации в октябре",
            "якорь": "VXX.US", "источник_карты": "картограф"}


COURT = {"NVDA.US": {"оценка": 2.8, "band": "средняя",
                     "критерии": {"mechanism_real": 1.0, "non_obviousness": 4.0},
                     "кто_против": "Продают инвесторы, уходящие в защитные активы (RSI 35, цена 212.08).",
                     "почему_возможность": "Перепроданность и неотыгранный ход +7.1%."}}


def _proto(top, court=None, money=None, built=None):
    court = COURT if court is None else court
    p = {"run_id": "ef_20261031T090001Z", "ts": "2026-10-31T09:00:01+00:00",
         "граф_отбор": {"топ_k": top, "провизорный_трек": top, "money_трек": [],
                        "разбор_идей": court, "суд_money": money or {}},
         "поток_идей": {"всего": len(top)}}
    if built is not None:
        p["каскады_в_компании"] = [{"chain_id": "c1", "узлов_построено": built}]
    return p


def test_idea_of_day_passes_lint_and_has_required_lines():
    top = [_entry("NVDA.US", 4, 0.0), _entry("FRO.US", 2, 0.1, edge=0.02), _entry("DAL.US", 3, 0.9, edge=-0.03)]
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top), **KW)
    assert viol == [] and text
    assert meta["idx"]["1"] == "NVDA.US" and meta["lead"] == "NVDA.US"
    assert text.startswith("Наблюдение дня — 31 октября. Nvidia (NVDA.US): возможный рост.")  # подпись 01.11: вариант А
    assert "Потенциал." in text and "шанс — как у средней идеи этого конвейера, около 48%" in text \
        and "матожидание около -0.2%" in text   # испытатель 01.11: сбываемость потока, не оценка идеи
    assert "если выигрыш и проигрыш одного размера" in text
    assert "Кто продаёт нам." in text and "Суд." in text and "Счёт честности." in text
    assert "Сверю к 7 ноября." in text            # 5 торговых дней → 7 календарных (как forecast._resolve_by)
    assert "Сила" not in text and "тех.id" not in text


def test_overplayed_lead_is_skipped_and_next_candidate_leads():
    """Блокер №1: узел с отыгранностью 1.0 (edge = перевёрнутый остаток) не защищаем — лид следующий."""
    top = [_entry("LNG.US", 3, 1.0, edge=-0.052, full=0.03), _entry("NVDA.US", 4, 0.0)]
    court = {**COURT, "LNG.US": {"оценка": 3.5, "band": "сильная", "критерии": {}}}
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top, court=court), **KW)
    assert viol == [] and meta["lead"] == "NVDA.US"
    assert text.startswith("Наблюдение дня — 31 октября. Nvidia (NVDA.US)")  # EV<0 → жанр наблюдения (подпись 01.11)
    lead, why, skipped = R.select_idea_day_lead(_proto(top, court=court))
    assert skipped == [("LNG.US", "рынок уже отыграл")]


def test_sign_conflict_edge_vs_full_is_skipped():
    top = [_entry("FRO.US", 2, 0.5, edge=-0.02, full=0.04), _entry("NVDA.US", 4, 0.0)]
    court = {**COURT, "FRO.US": {"оценка": 3.9, "band": "сильная", "критерии": {}}}   # FRO выше по баллу
    lead, why, skipped = R.select_idea_day_lead(_proto(top, court=court))
    assert lead[0]["актив"] == "NVDA.US" and skipped[0][1].startswith("остаток хода против механизма")


def test_all_overplayed_gives_honest_zero_with_reason():
    top = [_entry("LNG.US", 3, 1.0), _entry("DAL.US", 3, 0.95)]
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top), **KW)
    assert viol == [] and "рынок уже отыграл — защищать нечего" in text and meta["lead"] is None


def test_direction_and_size_from_full_move_remaining_separately():
    """Направление и ход — из полного расчётного хода; остаток (edge) — отдельной фразой."""
    top = [_entry("NVDA.US", 4, 0.4, edge=0.02, full=0.05)]
    text = R.format_idea_of_day(_proto(top), **KW)
    assert "возможный рост" in text and "Ход около 5.0% вверх, из них ещё не отыграно около 2.0%" in text  # жанр наблюдения при EV<0
    assert "рынок отыграл его частично — идея держится на остатке хода" in text


def test_chain_marks_each_link_and_nearer_unplayed_is_admitted():
    top = [_entry("NVDA.US", 4, 0.0), _entry("FRO.US", 2, 0.1, edge=0.02), _entry("DAL.US", 3, 0.9, edge=-0.03)]
    text = R.format_idea_of_day(_proto(top, built=5), **KW)
    assert "— второй шаг:" in text and "Frontline (FRO.US) — ещё не отыграно" in text
    assert "третий шаг:" in text and "рынок уже отыграл" in text
    assert "четвёртый шаг:" in text and "моё звено" in text
    assert "ближе к событию есть неотыгранные звенья (Frontline (FRO.US))" in text
    assert "Часть звеньев этой цепочки в выдачу не прошла" in text


def test_my_link_three_branches():
    unknown = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, None)]), **KW)
    assert "отыгранность не измерена — считать его неотыгранным не могу" in unknown
    nearer_unknown = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, 0.0), _entry("FRO.US", 2, None, edge=0.02)]), **KW)
    assert "у более близких звеньев отыгранность не измерена" in nearer_unknown
    clean = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, 0.0)]), **KW)
    assert "ближайшее из неотыгранных, что дошли до выдачи" in clean


def test_money_survived_lead_is_consistent_with_section11_card():
    """Регрессия №2: лид, прошедший денежный суд, НЕ говорит «на деньги не тянет» и «суд не пропустил ни одной»."""
    top = [_entry("NVDA.US", 4, 0.0)]
    money = {"NVDA.US": {"исход": "УСТОЯЛА", "балл": 3.4, "порог": 3.0,
                         "кто_против": "Продавцы ждут отката.", "почему_возможность": "Ход не отыгран."}}
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top, court={}, money=money), base_rate=BASE,
                                                   money_base={"hit": 0.6, "n": 40}, money_recent=1)
    assert viol == []
    assert "прошла денежный суд" in text.lower() and "отдельная карточка с кнопками" in text
    assert "шанс по денежному треку около 60%" in text
    assert "в деньги не пускаю" not in text and "не пропустил ни одной" not in text
    assert "денежные — лучше монетки" in text and "пропускал идеи — редко" in text


def test_foreign_text_is_cleaned_before_lint():
    """Регрессия №3: эмодзи/стрелки в чужом тексте (узел картографа, заголовок, судья) не валят пуш."""
    e = _entry("NVDA.US", 4, 0.0, node="Поставщики ↔ заказчики 📉 давление")
    e["событие"] = "Nasdaq падает 📉 — техи теряют капитализацию"
    court = {"NVDA.US": {**COURT["NVDA.US"], "кто_против": "Продают ✅ все подряд ↔ покупают немногие"}}
    text, meta, viol = R.idea_of_day_from_protocol(_proto([e], court=court), **KW)
    assert viol == [] and "📉" not in text and "↔" not in text and "✅" not in text


def test_english_headline_replaced_by_anchor_phrase():
    e = _entry("NVDA.US", 4, 0.0)
    e["событие"] = "US tech stocks lose two trillion in October - Reuters"
    text = R.format_idea_of_day(_proto([e]), **KW)
    assert "Повод. Импульс по фонд на индекс страха VIX (VXX.US)" in text   # испытатель 01.11: без двойных скобок


def test_numbers_cap_counts_outside_quotes_and_dates():
    text = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, 0.0, edge=0.02, full=0.05)]), **KW)
    nums = PL.count_numbers(text)
    assert len(nums) <= PL.IDEA_DAY_MAX_NUMBERS, nums
    assert not any("212" in n for n in nums)


def test_lint_blocks_bare_ticker_tech_id_and_too_many_numbers():
    bad = ("Идея дня. NVDA.US: ставка на рост.\nЧто это значит для тебя. x\n"
           "Ход 1%, 2%, 3%, 4%, 5%.\nЧто делать. y · тех.id ef_20261031T090001Z")
    joined = " | ".join(PL.lint_idea_of_day(bad))
    assert "тикеры без имени: NVDA.US" in joined and "тех.id" in joined
    assert "чисел вне цитат" in joined and "> 4" in joined


def test_company_name_digits_are_not_analysis_numbers():
    assert PL.count_numbers("Phillips 66 (PSX.US): ход около 2.4% вниз, шанс около 48%.") == ["2.4%", "48%"]


def test_unknown_company_is_marked_honestly_not_invented():
    top = [_entry("ZZZQ.US", 2, 0.0)]
    text, meta, viol = R.idea_of_day_from_protocol(_proto(top, court={}), **KW)
    assert viol == [] and f"ZZZQ.US — {PL.UNKNOWN_NAME_MARK}" in text
    assert "Суд. Идею сегодня не судили" in text and "Суд не отвечал" in text


def test_honest_zero_is_one_short_block_and_passes_lint():
    text, meta, viol = R.idea_of_day_from_protocol(_proto([], court={}), **KW)
    assert viol == [] and text.count("\n") <= 4 and "Сегодня стоящего нет" in text
    assert meta["idx"] == {} and meta["lead"] is None


def test_honesty_line_is_computed_in_words():
    assert "как монетка" in R._honesty_line(BASE, {"hit": None, "n": 0}, 0)
    assert "сверенных исходов пока мало" in R._honesty_line({"hit": 0.9, "n": 5}, None, None)
    assert "хуже монетки" in R._honesty_line({"hit": 0.40, "n": 100}, None, 0)
    assert "данных нет" in R._honesty_line(BASE, None, None)


def test_no_base_rate_is_honest():
    text = R.format_idea_of_day(_proto([_entry("NVDA.US", 4, 0.0)]), base_rate={"hit": None, "n": 0},
                                money_base={"hit": None, "n": 0}, money_recent=0)
    assert "шанс не измерен" in text


def test_top3_has_no_strength_score_and_shows_base(monkeypatch):
    monkeypatch.setattr(R, "track_base_rate", lambda *a, **k: BASE)
    text = R.format_top3(_proto([_entry("NVDA.US", 4, 0.0)]))
    assert "/5" not in text and "Сила" not in text
    assert "сбывались примерно в 48% случаев" in text and "Суд оценил как средняя" in text


def test_top3_court_jargon_goes_quoted_and_passes_lint(monkeypatch):
    """Перепроверка №3 (регрессия STOPLIST): жаргон СУДА («лонг», «edge», «чокпоинт») — чужие
    слова, идут цитатой «…» и не валят Топ-3 в тихий фолбэк."""
    monkeypatch.setattr(R, "track_base_rate", lambda *a, **k: BASE)
    court = {"NVDA.US": {"оценка": 2.8, "band": "средняя", "критерии": {},
                         "кто_против": "Шорт против нас: edge не подтверждён.",
                         "почему_возможность": "Лонг первого порядка, чокпоинт перегружен."}}
    text, _meta, viol = R.top3_from_protocol(_proto([_entry("NVDA.US", 4, 0.0)], court=court))
    assert text is not None and viol == [], viol
    assert "«Лонг первого порядка" in text          # чужие слова — в кавычках


def test_track_base_rate_reads_outcomes_kind(tmp_path):
    p = tmp_path / "o.jsonl"
    p.write_text('{"kind":"cascade_provisional","outcome":1}\n{"kind":"cascade_provisional","outcome":0}\n'
                 '{"kind":"calibration","outcome":1}\nмусор\n', encoding="utf-8")
    assert R.track_base_rate("cascade_provisional", path=p) == {"hit": 0.5, "n": 2}
    assert R.track_base_rate("edge_forward", path=p) == {"hit": None, "n": 0}


def test_money_seals_recent_counts_by_kind_and_window(tmp_path):
    p = tmp_path / "p.jsonl"
    p.write_text('{"kind":"cascade_money","sealed_at":"2099-01-01T00:00:00+00:00"}\n'
                 '{"kind":"cascade_money","sealed_at":"2000-01-01T00:00:00+00:00"}\n'
                 '{"kind":"cascade_provisional","sealed_at":"2099-01-01T00:00:00+00:00"}\n', encoding="utf-8")
    assert R._money_seals_recent(days=90, path=p) == 1
    assert R._money_seals_recent(days=90, path=tmp_path / "нет.jsonl") is None


# ── подпись владельца 01.11 (вариант А): два жанра подачи + знак-гард ────────────────
def test_genre_defend_when_ev_positive():
    """База с hit>0.5 → матожидание > 0 → жанр «Идея дня … защищаю»."""
    top = [_entry("NVDA.US", 4, 0.0)]
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top), base_rate={"hit": 0.6, "n": 100},
                                                 money_base=MBASE, money_recent=0)
    assert viol == []
    assert text.startswith("Идея дня — ") and "которую я сегодня защищаю" in text
    assert "Наблюдение дня" not in text


def test_genre_observe_when_ev_negative():
    """EV ≤ 0 → «Наблюдение дня», без слова «защищаю», с назначением проверки."""
    top = [_entry("NVDA.US", 4, 0.0)]
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top), **KW)
    assert viol == []
    assert text.startswith("Наблюдение дня — ") and "защищать нечего" in text
    assert "которую я сегодня защищаю" not in text
    assert "наблюдение, не ставка" in text


def test_sign_conflict_rejects_lead():
    """Испытатель №1: текст звена «давление на поставщиков» при расчётном ходе ВВЕРХ →
    лид НЕ проходит гард; единственный кандидат → честный ноль с причиной противоречия."""
    top = [_entry("NVDA.US", 4, 0.0, node="обвал оценок ставит под вопрос заказы — давление на поставщиков")]
    lead, why, skipped = R.select_idea_day_lead(_proto(top))
    assert lead is None
    assert skipped == [("NVDA.US", "текст звена против направления расчёта")]
    assert "противоречат сами себе" in why
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top), base_rate={"hit": 0.6, "n": 100},
                                                 money_base=MBASE, money_recent=0)
    assert viol == []
    assert "противоречат сами себе" in text


def test_sign_conflict_money_lead_still_observed_not_defended():
    """Money-ветка обходит гард лида (прошла суд §11), но при конфликте знака рендер
    честно НЕ защищает: «Наблюдение дня» + явная строка противоречия."""
    top = [_entry("NVDA.US", 4, 0.0, node="обвал оценок ставит под вопрос заказы — давление на поставщиков")]
    money = {"NVDA.US": {"исход": "УСТОЯЛА", "балл": 3.4, "порог": 3.0,
                         "кто_против": "Продавцы ждут отката.", "почему_возможность": "Ход не отыгран."}}
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top, court={}, money=money),
                                                 base_rate={"hit": 0.6, "n": 100},
                                                 money_base={"hit": 0.6, "n": 40}, money_recent=1)
    assert viol == []
    assert text.startswith("Наблюдение дня — ")
    assert "Противоречие внутри идеи" in text


def test_chain_gap_is_honest():
    """Исток(1) → четвёртый шаг без второго/третьего → строка о пропуске словами, без цифр."""
    top = [_entry("NVDA.US", 4, 0.0)]
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top), **KW)
    assert viol == []
    assert "до выдачи не дошёл — в цепочке пропуск" in text


def test_chain_numbered_in_order_with_gaps_in_place(  ):
    """Испытатель №1: цепочка целиком и по порядку (первый→N), дыры НА СВОЁМ МЕСТЕ,
    номера словами (линт чисел цел)."""
    top = [_entry("NVDA.US", 4, 0.0), _entry("CEG.US", 2, 0.1, node="заказы растут")]
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top), **KW)
    assert viol == []
    lines = [l for l in text.splitlines() if l.startswith("— ")]
    i1 = next(i for i, l in enumerate(lines) if l.startswith("— первый шаг (исток)"))
    i2 = next(i for i, l in enumerate(lines) if l.startswith("— второй шаг:"))
    i3 = next(i for i, l in enumerate(lines) if l.startswith("— третий шаг: до выдачи не дошёл"))
    i4 = next(i for i, l in enumerate(lines) if l.startswith("— четвёртый шаг:"))
    assert i1 < i2 < i3 < i4, lines


def test_no_full_move_prints_honest_remainder_label():
    """полный_ход отсутствует (боевые протоколы до фикса event_first) → «Неотыгранный остаток», не «Ход»."""
    top = [_entry("NVDA.US", 4, 0.0)]            # full=None по умолчанию
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top), **KW)
    assert viol == []
    assert "Неотыгранный остаток около 7.1%" in text and "Ход около 7.1%" not in text
    assert "Полный ход механизма в протоколе не посчитан" in text


def test_sign_conflict_headline_neutral():
    """Money-лид с конфликтом знака (обходит гард, идёт в рендер): заголовок не утверждает
    направление, пометка «направление спорно» на месте."""
    top = [_entry("NVDA.US", 4, 0.0, node="обвал оценок — давление на поставщиков")]
    money = {"NVDA.US": {"исход": "УСТОЯЛА", "балл": 3.4, "порог": 3.0,
                         "кто_против": "Продавцы ждут отката.", "почему_возможность": "Ход не отыгран."}}
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top, court={}, money=money),
                                                 base_rate={"hit": 0.6, "n": 100},
                                                 money_base={"hit": 0.6, "n": 40}, money_recent=1)
    assert viol == []
    assert "направление под вопросом" in text.splitlines()[0]
    assert "направление спорно" in text


def test_rsi_value_preserved_not_inverted():
    """П8: «RSI 35» в цитате судьи не заменяется выдуманной оценкой — число сохраняется."""
    court = {"NVDA.US": {"оценка": 2.8, "band": "средняя", "критерии": {},
                         "кто_против": "Продавцы на тренде.",
                         "почему_возможность": "достиг перепроданности (RSI 35), ход сохраняется"}}
    top = [_entry("NVDA.US", 4, 0.0)]
    text, _m, viol = R.idea_of_day_from_protocol(_proto(top, court=court), **KW)
    assert viol == []
    assert "RSI 35" in text and "перекупленности высокий" not in text
