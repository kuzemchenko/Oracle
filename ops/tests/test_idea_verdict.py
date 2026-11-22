# -*- coding: utf-8 -*-
"""Инвестиционный комитет (Q-вердикт-комитет, владелец 22.11). Герметично: сети нет, сектор — стаб."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

import idea_verdict as IV       # noqa: E402
import search_queue as SQ       # noqa: E402
import sector_view as SV        # noqa: E402
from mathlib import idea_score as IS    # noqa: E402

TODAY = datetime.date(2026, 11, 22)
RULES = IV.load_rules()


def _sector(against=False, status="попутный"):
    def fn(sym, direction, today):
        return {"против": against, "статус": status, "словами": f"сектор «тест» — {status}"}
    return fn


def _hyp(**over):
    """Гипотеза поиска, у которой собрано всё: 10 из 10."""
    d = {
        "факты": [{"текст": "факт 1", "url": "https://a.example/1", "опубликовано": "2026-11-20", "проверено": "2026-11-22"},
                  {"текст": "факт 2", "url": "https://b.example/2", "опубликовано": "2026-11-21", "проверено": "2026-11-22"}],
        "механизм": "контракт добавляет выручку сверх ожиданий аналитиков и поднимает прибыль",
        "chain": [{"text": "событие → заказ", "quality": "confirmed", "url": "https://a.example/1"},
                  {"text": "заказ → выручка компании", "quality": "confirmed", "url": "https://b.example/2"}],
        "materiality": {"effect_profit_per_year": 300e6, "years": 3, "net_income": 1000e6, "market_cap": 4000e6},
        "durability": {"reason": "контракт подписан на пять лет с фиксированной ценой", "url": "https://c.example/3"},
        "market_view": {"kind": "magnitude", "metric": "выручка 2027, $ млрд", "market": {"value": 10.0, "source": "консенсус EODHD"},
                        "ours": {"value": 11.5}, "why_different": "аналитики ещё не внесли подписанный контракт в свои модели",
                        "основано_на": [1, 2]},
        "precedent": {"what": "похожий контракт в 2023 году", "amplitude": "+25%", "url": "https://d.example/4"},
        "сценарии": {"цена": 100.0, "дата_цены": "2026-11-20", "источник_цены": "EODHD",
                     "базовый": {"цена": 130.0, "основание": "прибыль на акцию 6.5 × мультипликатор 20"},
                     "неблагоприятный": {"цена": 88.0, "основание": "контракт сорван — возврат к уровню до новости"}},
        "направление": "long", "горизонт": "2027-01-15",
        "критика": {"семейство": "openai", "возражения": [{"текст": "заказ могут отменить", "существенное": True,
                                                           "ответ": "в контракте штраф за отмену — ссылка на 8-K"}]},
    }
    d.update(over)
    return {"id": "h1", "тикер": "AAA", "получатель": "Альфа — делает станки", "вердикт": "open", "данные": d}


def test_full_hypothesis_is_bet_candidate():
    v = IV.verdict_for_hypothesis(_hyp(), TODAY, RULES, _sector())
    assert v["балл"] == 10 and v["решение"] == IS.BET and not v["брак"], v["не_хватает"]
    txt = IV.render(v)
    assert "10 из 10" in txt and "кандидат на маленькую ставку" in txt and "не вероятность" in txt


def test_each_missing_piece_costs_exactly_one_point():
    """Убираем по одному доказательству — балл падает ровно на 1, и пункт назван."""
    cases = {"chain": "КАНАЛ", "materiality": "РАЗМЕР", "durability": "СРОК", "precedent": "ПРЕЦЕДЕНТ", "критика": "СУД"}
    for key, code in cases.items():
        v = IV.verdict_for_hypothesis(_hyp(**{key: None}), TODAY, RULES, _sector())
        assert v["балл"] == 9, (key, v["балл"])
        assert [p["код"] for p in v["не_хватает"]] == [code]
    v = IV.verdict_for_hypothesis(_hyp(), TODAY, RULES, _sector(against=True, status="корректируется"))
    assert v["балл"] == 9 and v["не_хватает"][0]["код"] == "СЕКТОР"


def test_sector_not_measured_is_not_counted():
    v = IV.verdict_for_hypothesis(_hyp(), TODAY, RULES, lambda *a: {"против": None, "словами": "не измерено"})
    assert v["балл"] == 9 and v["не_хватает"][0]["зачтён"] is None
    v = IV.verdict_for_hypothesis(_hyp(), TODAY, RULES, sector_fn=False)       # сектор не запрашивали
    assert v["балл"] == 9


def test_market_view_without_numbers_or_tiny_gap_not_counted():
    mv = dict(_hyp()["данные"]["market_view"], ours={"value": 10.2})            # +2% — почти как рынок
    v = IV.verdict_for_hypothesis(_hyp(market_view=mv), TODAY, RULES, _sector())
    assert "РЫНОК" in [p["код"] for p in v["не_хватает"]]
    mv = dict(_hyp()["данные"]["market_view"], kind="flow")
    v = IV.verdict_for_hypothesis(_hyp(market_view=mv), TODAY, RULES, _sector())
    assert "РЫНОК" in [p["код"] for p in v["не_хватает"]]
    assert "ПОЧЕМУ" not in [p["код"] for p in v["не_хватает"]]                   # причина названа — п.5 зачтён


def test_asymmetry_threshold_from_rules():
    sc = dict(_hyp()["данные"]["сценарии"], базовый={"цена": 115.0, "основание": "прибыль 5.75 × мультипликатор 20"})
    v = IV.verdict_for_hypothesis(_hyp(сценарии=sc), TODAY, RULES, _sector())    # 15% к 12% = 1.25 к 1
    p8 = [p for p in v["пункты"] if p["код"] == "ВЫИГРЫШ"][0]
    assert p8["зачтён"] is False and "1.25 к 1" in p8["почему"]


def test_no_exit_rule_blocks_bet_even_with_points():
    sc = dict(_hyp()["данные"]["сценарии"])
    sc.pop("неблагоприятный")
    v = IV.verdict_for_hypothesis(_hyp(сценарии=sc), TODAY, RULES, _sector())
    assert v["решение"] != IS.BET and any("правила выхода" in b for b in v["брак"])


def test_expired_horizon_and_uncomputed_target():
    v = IV.verdict_for_hypothesis(_hyp(горизонт="2026-11-01"), TODAY, RULES, _sector())
    assert "СОБЫТИЕ" in [p["код"] for p in v["не_хватает"]]
    sc = dict(_hyp()["данные"]["сценарии"], базовый={"цена": 130.0, "основание": "бумага просто должна вырасти сильно"})
    v = IV.verdict_for_hypothesis(_hyp(сценарии=sc), TODAY, RULES, _sector())
    assert "СОБЫТИЕ" in [p["код"] for p in v["не_хватает"]]                      # основание без единого числа


def test_unanswered_material_objection_fails_court_point():
    cr = {"семейство": "openai", "возражения": [{"текст": "спрос слабеет", "существенное": True, "ответ": ""}]}
    v = IV.verdict_for_hypothesis(_hyp(критика=cr), TODAY, RULES, _sector())
    p = [p for p in v["пункты"] if p["код"] == "СУД"][0]
    assert p["зачтён"] is False and "без ответа" in p["почему"]


def test_rejected_hypothesis_goes_to_archive_with_reason():
    h = _hyp()
    h["вердикт"] = "rejected"
    h["отказ"] = {"вид": "priced_in", "причина": "бумага выросла на 16% за два дня после отчёта"}
    v = IV.verdict_for_hypothesis(h, TODAY, RULES, _sector())
    assert v["решение"] == IS.ARCHIVE and v["балл"] is None and "уже в цене" in v["причина"]
    assert "архив" in IV.render(v) and "16%" in IV.render(v)


def test_nested_stage_data_is_read():
    """21.11: модель вложила данные в ключ «быстрая_проверка» — комитет и поиск читают их оттуда."""
    h = _hyp()
    h["данные"] = {"быстрая_проверка": h["данные"]}
    v = IV.verdict_for_hypothesis(h, TODAY, RULES, _sector())
    assert v["балл"] == 10


def test_search_verdicts_sorted_and_rendered():
    weak = _hyp(chain=None, materiality=None, durability=None, precedent=None, критика=None)
    weak["id"], weak["тикер"], weak["получатель"] = "h2", "BBB", "Бета — делает чипы"
    dead = _hyp()
    dead.update(id="h3", тикер="CCC", получатель="Гамма — банк", вердикт="rejected",
                отказ={"вид": "mechanism_refuted", "причина": "выручка от этого сегмента — меньше одного процента"})
    s = {"id": "s1", "тема": "тестовая тема", "гипотезы": [dead, weak, _hyp()]}
    vs = IV.search_verdicts(s, TODAY, RULES, _sector())
    assert [v["тикер"] for v in vs] == ["AAA", "BBB", "CCC"] and vs[1]["балл"] == 5
    txt = IV.render_search(vs, s["тема"])
    assert "Альфа (AAA) — 10 из 10" in txt and "Бета (BBB) — 5 из 10, архив" in txt and "Гамма (CCC) (механизм опровергнут)" in txt


# ── карточка охотника ──
def _card():
    return json.loads((ROOT / "reports" / "ideas" / "idea_2026-11-03_DHT.card.json").read_text(encoding="utf-8"))


def test_old_hunter_card_scored_by_positive_evidence_only():
    """Карточка DHT от 03.11 (до правил «рынок ждёт X»): зачтены только пункты с доказательством, не «по отсутствию
    замечаний»; суд не проводился — п.10 не зачтён; вторая проверка PASS добавляет ровно один балл."""
    d = datetime.date(2026, 11, 3)
    v = IV.verdict_for_card(_card(), d, RULES, sector_fn=_sector())
    codes = {p["код"] for p in v["пункты"] if p["зачтён"] is True}
    assert codes == {"ПОЧЕМУ", "ПРЕЦЕДЕНТ", "СЕКТОР", "ВЫИГРЫШ"}, codes
    assert v["решение"] == IS.ARCHIVE and v["имя"] and v["тикер"] == "DHT.US"
    v2 = IV.verdict_for_card(_card(), d, RULES, court={"verdict": "PASS"}, sector_fn=_sector())
    assert v2["балл"] == v["балл"] + 1
    v3 = IV.verdict_for_card(_card(), d, RULES, court={"verdict": "FATAL"}, sector_fn=_sector())
    assert v3["балл"] == v["балл"] and any("не связан" in b for b in v3["брак"])


def test_broken_card_does_not_score_by_absence():
    """Пустая карточка: шлагбаум выходит рано, замечаний по пунктам нет — балл обязан быть 0, а не 10."""
    v = IV.verdict_for_card({"ticker": "ZZZ.US"}, TODAY, RULES, sector_fn=False)
    assert v["балл"] == 0 and v["решение"] == IS.ARCHIVE and v["брак"]


def test_blockers_are_plain_words():
    v = IV.verdict_for_card({"ticker": "ZZZ.US"}, TODAY, RULES, sector_fn=False)
    assert all("§" not in b and "spec9" not in b for b in v["брак"])
    assert "Мешает ставке" in IV.render(v)


# ── запись и счёт метода ──
def test_save_and_track_by_band(tmp_path):
    good = IV.verdict_for_hypothesis(_hyp(), TODAY, RULES, _sector())
    weak = IV.verdict_for_hypothesis(_hyp(chain=None, materiality=None, durability=None, precedent=None, критика=None),
                                     TODAY, RULES, _sector())
    weak["тикер"] = "BBB"
    IV.save(good, tmp_path)
    IV.save(weak, tmp_path)
    IV.save(dict(good, балл=3), tmp_path)                    # повторный вердикт по той же бумаге — в счёт идёт ПЕРВЫЙ
    recs = IV.saved(tmp_path)
    assert len(recs) == 3 and recs[0]["зачтены"] and recs[0]["цена"] == 100.0

    class Prices:
        def series(self, sym, date_from):
            end = {"AAA.US": 110.0, "BBB.US": 95.0}.get(sym, 100.0)
            return [(date_from, 100.0), ("2026-12-20", end)]

    tr = IV.track(recs, Prices(), RULES)
    assert tr[IS.BET]["n"] == 1 and abs(tr[IS.BET]["средняя"] - 0.10) < 1e-9
    assert tr[IS.ARCHIVE]["n"] == 1 and abs(tr[IS.ARCHIVE]["средняя"] + 0.05) < 1e-9 and tr[IS.WAIT]["n"] == 0
    line = IV.track_line(tr, RULES)
    assert "8+" in line and "Рано судить" in line and "+10.0%" in line


def test_board_orders_by_score(tmp_path):
    sd = tmp_path / "searches"
    sd.mkdir()
    weak = _hyp(chain=None, materiality=None, durability=None)          # 7 из 10 — лист ожидания
    weak["id"], weak["тикер"] = "h2", "BBB"
    s = {"id": "s_x", "тема": "тема", "статус": "paused", "гипотезы": [weak, _hyp()]}
    (sd / "s_x.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    rows = IV.board(out_dir=tmp_path / "v", search_dir=sd, today=TODAY, rules=RULES, sector_fn=_sector())
    assert [r["тикер"] for r in rows] == ["AAA", "BBB"]
    txt = IV.render_board(rows)
    assert txt.index("Кандидаты на маленькую ставку") < txt.index("Лист ожидания")


# ── сектор: источники ──
def _closes(vals):
    d0 = datetime.date(2025, 9, 1)
    return [((d0 + datetime.timedelta(days=i)).isoformat(), v) for i, v in enumerate(vals)]


def test_sector_for_ticker_manual_list_and_direction():
    rules = RULES["сектор"]
    down = [100.0] * 200 + [100 - i * 0.3 for i in range(1, 71)]
    fetch = {"closes": lambda sym, today: _closes(down if sym == "CIBR.US" else [100.0] * 270),
             "general": lambda sym: None}
    r = SV.for_ticker("S", "вверх", TODAY, rules, fetch, use_cache=False)
    assert r["фонд"] == "CIBR.US" and r["статус"] == "корректируется" and r["против"] is True
    assert "кибербезопасность" in r["словами"]
    assert SV.for_ticker("S.US", "вниз", TODAY, rules, fetch, use_cache=False)["против"] is False


def test_sector_from_eodhd_industry_then_sector_then_unknown():
    rules = RULES["сектор"]
    flat = lambda sym, today: _closes([100.0] * 270)        # noqa: E731
    f = {"closes": flat, "general": lambda sym: {"sector": "Technology", "industry": "Semiconductor Equipment"}}
    assert SV.fund_for("QQQQ.US", rules, f, use_cache=False)["фонд"] == "SMH.US"
    f = {"closes": flat, "general": lambda sym: {"sector": "Energy", "industry": "Oil & Gas Midstream"}}
    assert SV.fund_for("QQQQ.US", rules, f, use_cache=False)["фонд"] == "XLE.US"
    f = {"closes": flat, "general": lambda sym: {"sector": "Information Technology", "industry": "Semiconductors | Solar"}}
    assert SV.fund_for("QQQQ.US", rules, f, use_cache=False)["фонд"] == "XLK.US"      # солнечные панели — не чипы (FSLR)
    f = {"closes": flat, "general": lambda sym: None}
    r = SV.for_ticker("QQQQ.US", "вверх", TODAY, rules, f, use_cache=False)
    assert r["против"] is None and "не определён" in r["словами"]


def test_sector_split_gap_is_not_a_crash(monkeypatch):
    """22.11: фонд ITA после дробления 05.11 в базе «−68% за день» → ложное «сектор −70%». Разрыв ряда = взять свежий
    ряд у поставщика; разрыв и там — «не измерено»."""
    gap = _closes([210.0] * 250 + [68.0] * 20)
    assert SV.has_gap(gap) and not SV.has_gap(_closes([100.0, 104.0, 99.0]))
    monkeypatch.setattr(SV, "_db_closes", lambda sym, today, db_path=None: gap)
    monkeypatch.setattr(SV, "_api_closes", lambda sym, today: _closes([70.0] * 250 + [68.0] * 20))
    rows = SV.fetch_closes("ITA.US", TODAY)
    assert rows and not SV.has_gap(rows) and rows[0][1] == 70.0
    monkeypatch.setattr(SV, "_api_closes", lambda sym, today: None)
    assert SV.fetch_closes("ITA.US", TODAY) is None


def test_sector_empty_answer_is_not_cached(tmp_path):
    rules = RULES["сектор"]
    cp = tmp_path / "c.json"
    f = {"closes": lambda sym, today: None, "general": lambda sym: {"sector": None, "industry": ""}}
    assert "причина" in SV.fund_for("QQQQ.US", rules, f, cache_path=cp)
    assert not cp.exists()
    f["general"] = lambda sym: {"sector": "Energy", "industry": "Oil & Gas Midstream"}
    assert SV.fund_for("QQQQ.US", rules, f, cache_path=cp)["фонд"] == "XLE.US" and "QQQQ.US" in json.loads(cp.read_text())


def test_sector_percent_words():
    from mathlib import sector_rules as SR
    assert [SR._pct(x) for x in (0.004, -0.004, 0.05, -0.12)] == ["0%", "0%", "+5%", "−12%"]


def test_sector_map_render_groups():
    rules = dict(RULES["сектор"], карта=["XLK.US", "XLE.US"])
    up = [100.0] * 200 + [100 + i * 0.1 for i in range(1, 71)]
    down = [100.0] * 200 + [100 - i * 0.3 for i in range(1, 71)]
    fetch = {"closes": lambda sym, today: _closes({"XLK.US": down, "XLE.US": up}.get(sym, [100.0] * 270))}
    txt = SV.render(SV.sector_map(TODAY, rules, fetch))
    assert txt.index("Попутный") < txt.index("Корректируется") and "технологии (XLK)" in txt and "не прогноз" in txt


# ── поиск: баг валидатора 19–21.11 ──
def test_quick_check_accepts_mechanism_text_in_any_field():
    base = {"факты": [{"url": "https://a.example"}], "цена": 10.0, "дата_цены": "2026-11-20", "потенциал_пп": 30,
            "риски": ["риск"], "инструмент_доступен": True}
    txt = "интеграция займёт конкурента на год — клиенты переносят контракты"
    assert SQ.quick_check_gaps({**base, "механизм_проверен": True, "механизм": txt}) == []
    assert SQ.quick_check_gaps({**base, "причинный_механизм": txt}) == []
    assert SQ.quick_check_gaps({**base, "механизм_проверен": txt}) == []        # прежний способ тоже работает
    g = SQ.quick_check_gaps({**base, "механизм_проверен": True})
    assert len(g) == 1 and "ТЕКСТОМ" in g[0]
    assert SQ.quick_check_gaps({"быстрая_проверка": {**base, "механизм": txt}, "механизм_проверен": True}) == []


def test_advance_passes_quick_check_with_nested_data(tmp_path):
    s = SQ.start("тема поиска про контракт", "telegram", d=tmp_path)
    h, _ = SQ.add_hypothesis(s, "тезис про контракт и выручку компании", "AAA", "контракт → выручка → прибыль компании")
    SQ.advance(s, h["id"], "pass")
    assert h["этап"] == "быстрая_проверка"
    d = {"быстрая_проверка": {"факты": [{"текст": "ф", "url": "https://a.example"}], "механизм_проверен": True,
                              "механизм": "контракт добавляет девятнадцать процентов к выручке", "цена": 10.0,
                              "дата_цены": "2026-11-20", "потенциал_пп": 30, "риски": ["срыв"], "инструмент_доступен": True}}
    _h, msg = SQ.advance(s, h["id"], "pass", d)
    assert h["этап"] == "углубление", msg
    assert "быстрая_проверка" not in h["данные"] and h["данные"]["цена"] == 10.0


# ── подключение к агенту Telegram: всё за флагом idea_verdict_push (правило 01.11) ──
def _agent_with_search(tmp_path, hyps, status="running", pause=None, spent=1.0):
    import bot_agent as A
    sd = tmp_path / "searches"
    sd.mkdir(exist_ok=True)
    s = SQ.start("тема поиска про контракт и выручку", "telegram", d=sd)
    s["гипотезы"] = hyps
    s["бюджет"]["потрачено_usd"] = spent
    s["статус"], s["пауза"] = status, pause
    SQ.save(s, sd)
    tools = A.Tools()
    tools.search_dir, tools.verdict_dir, tools.search_id, tools.search_worked = sd, tmp_path / "verdicts", s["id"], True
    ag = A.Agent(post=lambda body: None, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda x: None)
    return A, ag, s


def _stub_sector(monkeypatch):
    monkeypatch.setattr(SV, "for_ticker", lambda sym, direction="вверх", today=None, **kw: {"против": False, "словами": "сектор «тест» — попутный"})


def test_agent_flag_off_changes_nothing(tmp_path, monkeypatch):
    A, ag, s = _agent_with_search(tmp_path, [_hyp()])
    monkeypatch.setattr(A, "_flag", lambda name: False)
    assert "committee_verdict" not in {t["function"]["name"] for t in A.tool_specs()}
    assert ag._committee_hint(s) == "" and ag._committee_tail(s) is None
    assert "не включён" in ag.tools.committee_verdict()
    line = ag._close_search_turn()
    assert "комитет" not in line.lower() and not (tmp_path / "verdicts").exists()


def test_agent_flag_on_hint_tail_and_tool(tmp_path, monkeypatch):
    weak = _hyp(chain=None, materiality=None, durability=None)
    A, ag, s = _agent_with_search(tmp_path, [weak])
    monkeypatch.setattr(A, "_flag", lambda name: True)
    _stub_sector(monkeypatch)
    assert "committee_verdict" in {t["function"]["name"] for t in A.tool_specs()}
    hint = ag._committee_hint(s)
    assert "7 из 10" in hint and "КАНАЛ" in hint and "на исходе" not in hint
    out = json.loads(ag.tools.committee_verdict("h1"))
    assert out["вердикты"][0]["балл"] == 7 and len(out["вердикты"][0]["не_зачтено"]) == 3
    line = ag._close_search_turn()                       # ответ кончился — пауза «turn»: короткая строка, без записи
    assert "Лучший кандидат сейчас: Альфа (AAA) — 7 из 10, лист ожидания" in line
    assert not (tmp_path / "verdicts").exists()


def test_agent_budget_tail_reserves_for_verdict(tmp_path, monkeypatch):
    A, ag, s = _agent_with_search(tmp_path, [_hyp(chain=None)], spent=8.0)       # осталось 20% бюджета поиска
    monkeypatch.setattr(A, "_flag", lambda name: True)
    _stub_sector(monkeypatch)
    assert "новых гипотез не добавляй" in ag._committee_hint(s)


def test_agent_final_pause_gives_full_verdict_and_saves(tmp_path, monkeypatch):
    dead = _hyp()
    dead.update(id="h2", тикер="CCC", получатель="Гамма — банк", вердикт="rejected",
                отказ={"вид": "priced_in", "причина": "бумага уже выросла на шестнадцать процентов"})
    A, ag, s = _agent_with_search(tmp_path, [_hyp(), dead], status="paused",
                                  pause={"вид": "budget", "причина": "бюджет поиска исчерпан"}, spent=10.2)
    monkeypatch.setattr(A, "_flag", lambda name: True)
    _stub_sector(monkeypatch)
    ag.tools.search_worked = False
    line = ag._close_search_turn()
    assert "Итог комитета" in line and "Вердикт по Альфа (AAA): 10 из 10 — кандидат на маленькую ставку" in line
    assert "В архиве: Гамма (CCC) (эффект уже в цене)" in line
    recs = IV.saved(tmp_path / "verdicts")
    assert len(recs) == 1 and recs[0]["тикер"] == "AAA" and recs[0]["балл"] == 10
