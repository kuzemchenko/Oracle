# -*- coding: utf-8 -*-
"""VQ-4 (задание владельца 09.11): поиск идеи доводится до результата.

Приёмка: первые два кандидата отклонены → поиск переходит к третьему; бюджет ограничил работу → очередь
сохранена, успех не засчитан; отвергнутый тезис не возвращается под новым названием без новых данных;
слабый кандидат не проходит проверку кодом; агент не может закончить ответ, пока поиск идёт."""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

from ops import search_queue as SQ   # noqa: E402
import bot_agent as A                # noqa: E402

TODAY = datetime.date.today().isoformat()
OLD = (datetime.date.today() - datetime.timedelta(days=20)).isoformat()


def _facts(checked=TODAY):
    return [{"текст": "Компания подписала контракт на поставку на $400 млн до 2028 года",
             "url": "https://www.sec.gov/x", "опубликовано": "2026-11-05", "проверено": checked},
            {"текст": "Консенсус выручки на 2027 год — $2.1 млрд, 9 аналитиков (EODHD Earnings::Trend)",
             "url": "https://eodhd.com/api/fundamentals/X.US", "опубликовано": "2026-11-08", "проверено": checked}]


def worthy_data(price_date=TODAY, checked=TODAY):
    return {
        "факты": _facts(checked), "механизм_проверен": "контракт добавляет ~19% к выручке 2027 года сверх консенсуса",
        "цена": 20.0, "дата_цены": price_date, "потенциал_пп": 25, "риски": ["срыв поставок"],
        "инструмент_доступен": True,
        "market_view": {"kind": "magnitude", "metric": "выручка 2027, $ млрд",
                        "market": {"value": 2.1, "source": "консенсус EODHD Earnings::Trend"},
                        "ours": {"value": 2.5},
                        "why_different": "консенсус не включает контракт от 05.11: 9 из 9 оценок датированы до публикации",
                        "основано_на": [1, 2]},
        "сценарии": {"цена": 20.0, "дата_цены": price_date, "источник_цены": "price_snapshot EODHD close",
                     "базовый": {"цена": 25.0, "основание": "P/S 2.0 × выручка $2.5 млрд / 200 млн акций"},
                     "неблагоприятный": {"цена": 16.0, "основание": "контракт сорван, P/S сжимается до 1.6"}},
        "направление": "long", "горизонт": "2027-03-31",
        "условия_реализации": ["отчёт за 4 кв. подтверждает отгрузки"],
        "условия_опровержения": ["выручка 4 кв. ниже $480 млн", "расторжение контракта до 2026-12-31"],
        "критика": {"семейство": "openai", "возражения": [
            {"текст": "контракт может быть отложен", "существенное": True,
             "ответ": "график поставок зафиксирован в 8-K, штрафы за перенос у покупателя"}]},
    }


def _run_to_found(s, hid, data=None):
    data = data or worthy_data()
    msgs = []
    for _ in range(len(SQ.STAGES)):
        _h, m = SQ.advance(s, hid, "pass", data)
        msgs.append(m)
        if s["статус"] == SQ.FOUND:
            break
    return msgs


def test_two_rejections_move_to_third_candidate(tmp_path):
    s = SQ.start("медь", "telegram", d=tmp_path)
    h1, _ = SQ.add_hypothesis(s, "Медные шахты выигрывают от дефицита концентрата", "FCX", "дефицит концентрата поднимает цену меди")
    h2, _ = SQ.add_hypothesis(s, "Кабельщики выигрывают от строительства сетей", "PRY.MI", "заказы на кабели для сетей растут")
    SQ.advance(s, h1["id"], "reject", reject_kind="priced_in", reason="рост цены меди уже в цене акций майнеров (+40% за год)")
    SQ.advance(s, h2["id"], "reject", reject_kind="mechanism_refuted", reason="портфель заказов кабельщика снизился в 3 кв.")
    assert s["статус"] == SQ.RUNNING                         # два отказа поиск НЕ завершают
    nxt = SQ.next_action(s)
    assert nxt["шаг"] == "направление" and "получател" in nxt["текст"]   # priced_in → другие получатели прибыли
    h3, err = SQ.add_hypothesis(s, "Переработчики лома получают маржу от разрыва цен", "SCHN",
                                "разрыв цены лома и катодной меди расширяет маржу переработки", circle=2,
                                direction_id=nxt["направление"])
    assert err is None and h3["id"] == "h3"
    assert SQ.next_action(s)["гипотеза"] == "h3"
    msgs = _run_to_found(s, "h3")
    assert s["статус"] == SQ.FOUND and s["итог"]["тикер"] == "SCHN", msgs
    SQ.save(s, tmp_path)
    assert SQ.load(s["id"], tmp_path)["статус"] == SQ.FOUND


def test_rejected_thesis_cannot_return_under_new_name(tmp_path):
    s = SQ.start("танкеры", "telegram", d=tmp_path)
    h, _ = SQ.add_hypothesis(s, "DHT выигрывает от ставок фрахта VLCC", "DHT", "ставки фрахта VLCC растут из-за санкций")
    SQ.advance(s, h["id"], "reject", reject_kind="priced_in", reason="ставки уже в консенсусе на 2027 год, +35%")
    again, err = SQ.add_hypothesis(s, "DHT Holdings — бенефициар санкционного фрахта", "DHT.US",
                                   "санкции растут ставки фрахта VLCC")
    assert again is None and "отвергнутый" in err
    nd = [{"текст": "Минфин США 08.11 ввёл санкции против ещё 40 танкеров теневого флота",
           "url": "https://home.treasury.gov/news/x", "опубликовано": "2026-11-08"}]
    back, err2 = SQ.add_hypothesis(s, "DHT Holdings — бенефициар санкционного фрахта", "DHT.US",
                                   "санкции растут ставки фрахта VLCC", new_data=nd)
    assert err2 is None and back["новые_данные"] == nd


def test_waiting_for_data_does_not_block_other_directions(tmp_path):
    s = SQ.start("уран", "telegram", d=tmp_path)
    h1, _ = SQ.add_hypothesis(s, "Обогатители выигрывают от запрета российского урана", "LEU", "запрет импорта обогащённого урана")
    h2, _ = SQ.add_hypothesis(s, "Конвертеры урана получают премию за мощность", "CCJ", "дефицит конверсии UF6 поднимает цену")
    SQ.advance(s, h1["id"], "needs_data", needs=["отчёт LEU за 3 кв. 2026 (выйдет 12.11)"])
    assert SQ.next_action(s)["гипотеза"] == "h2"                    # ждём отчёта по h1 — h2 не стоит
    SQ.advance(s, h2["id"], "reject", reject_kind="instrument_unfit", reason="CCJ — диверсифицирован, эффект размыт")
    nxt = SQ.next_action(s)
    assert nxt["шаг"] == "направление" and "способ" in nxt["текст"]  # другой способ выразить идею
    s["направления"][0]["взято"] = True
    assert SQ.next_action(s)["шаг"] == "данные"                     # потом — добыть данные по h1
    assert s["статус"] == SQ.RUNNING


def test_quick_check_cuts_weak_before_long_work(tmp_path):
    s = SQ.start("x", "telegram", d=tmp_path)
    h, _ = SQ.add_hypothesis(s, "Компания выигрывает от нового закона о сетях", "XYZ", "закон даёт субсидии на сети")
    SQ.advance(s, h["id"], "pass")                       # генерация
    _h, msg = SQ.advance(s, h["id"], "pass", {"факты": [], "цена": 10})
    assert "не пройдена" in msg and h["этап"] == "быстрая_проверка"
    for need in ("источник", "механизм", "дат", "потенциал", "риски", "инструмент"):
        assert need in msg


def test_worthy_rejects_weak_or_stale_candidates():
    h = {"данные": worthy_data()}
    assert SQ.worthy_gaps(h, TODAY) == []
    stale = {"данные": worthy_data(price_date=OLD, checked=OLD)}
    gaps = " ".join(SQ.worthy_gaps(stale, TODAY))
    assert "пересчитать от актуальной" in gaps and "перепроверить" in gaps
    d = worthy_data()
    d["вероятность"] = 0.7
    d["основание_вероятности"] = "по моему опыту"
    assert any("придуманный" in g for g in SQ.worthy_gaps({"данные": d}, TODAY))
    d = worthy_data()
    d["сценарии"]["неблагоприятный"]["цена"] = 21.0
    assert any("не показывает потерь" in g for g in SQ.worthy_gaps({"данные": d}, TODAY))
    d = worthy_data()
    d["market_view"]["основано_на"] = []
    d["market_view"]["why_different"] = "низкий P/E и акция уже выросла на 30%, модель считает, что дальше рост"
    gaps = " ".join(SQ.worthy_gaps({"данные": d}, TODAY))
    assert "не привязан" in gaps and "не довод" in gaps
    d = worthy_data()
    d["сценарии"]["базовый"]["основание"] = "до исторического максимума — выше не пойдёт, это потолок"
    assert any("потолок" in g for g in SQ.worthy_gaps({"данные": d}, TODAY))
    d = worthy_data()
    d["критика"]["возражения"][0]["ответ"] = ""
    assert any("возражения" in g for g in SQ.worthy_gaps({"данные": d}, TODAY))


def test_evaluation_stage_refuses_to_pass_unworthy(tmp_path):
    s = SQ.start("x", "telegram", d=tmp_path)
    h, _ = SQ.add_hypothesis(s, "Компания выигрывает от нового закона о сетях", "XYZ", "закон даёт субсидии на сети")
    d = worthy_data()
    d.pop("критика")
    for _ in range(4):
        SQ.advance(s, h["id"], "pass", d)
    assert h["этап"] == "оценка"
    _h, msg = SQ.advance(s, h["id"], "pass")
    assert "не достоин" in msg and "критика" in msg and s["статус"] == SQ.RUNNING


def test_budget_exhaustion_pauses_keeps_queue_and_is_not_success(tmp_path):
    s = SQ.start("медь", "telegram", budget_usd=1.0, d=tmp_path)
    SQ.add_hypothesis(s, "Медные шахты выигрывают от дефицита концентрата", "FCX", "дефицит концентрата поднимает цену меди")
    SQ.charge(s, 0.6)
    assert s["статус"] == SQ.RUNNING
    SQ.charge(s, 0.5)
    assert s["статус"] == SQ.PAUSED and s["пауза"]["вид"] == "budget"
    assert s["пауза"]["очередь"]["открыто"] == 1 and len(s["гипотезы"]) == 1   # очередь сохранена
    SQ.save(s, tmp_path)
    s2 = SQ.load(s["id"], tmp_path)
    _s, err = SQ.resume(s2)
    assert err and s2["статус"] == SQ.PAUSED                      # сам бюджет не поднимает
    line = SQ.status_line(s2)
    assert "приостановлен" in line and "сам не повышаю" in line
    SQ.raise_budget(s2, 3.0, "владелец: подними до 3 долларов")
    _s, err = SQ.resume(s2)
    assert err is None and s2["статус"] == SQ.RUNNING
    try:
        SQ.raise_budget(s2, 10, "")
        raise AssertionError("бюджет поднят без слова владельца")
    except ValueError:
        pass


def test_cancel_only_with_owner_words(tmp_path):
    s = SQ.start("x", "telegram", d=tmp_path)
    try:
        SQ.cancel(s, "")
        raise AssertionError
    except ValueError:
        pass
    SQ.cancel(s, "владелец: отмени поиск")
    assert s["статус"] == SQ.CANCELLED and SQ.next_action(s)["шаг"] == "отменён"


# ── агент: поиск нельзя закончить словами «нечего», пока есть ресурс ──────────────────
def _msg(content=None, calls=None, cost=0.01):
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = [{"id": f"c{i}", "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                           for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": m, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"cost": cost}}


class FakeLLM:
    def __init__(self, script, default="Готово."):
        self.script, self.bodies, self.default = list(script), [], default

    def __call__(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        return self.script.pop(0) if self.script else _msg(self.default)


def test_agent_continues_after_two_rejections_to_found(tmp_path):
    tools = A.Tools()
    tools.search_dir = tmp_path
    data = worthy_data()
    script = [
        _msg(calls=[("search_start", {"topic": "медь"})]),
        _msg(calls=[("search_add", {"thesis": "Медные шахты выигрывают от дефицита концентрата", "ticker": "FCX",
                                    "mechanism": "дефицит концентрата поднимает цену меди"}),
                    ("search_add", {"thesis": "Кабельщики выигрывают от строительства сетей", "ticker": "PRY",
                                    "mechanism": "заказы на кабели для сетей растут"})]),
        _msg(calls=[("search_step", {"hyp_id": "h1", "outcome": "reject", "reject_kind": "priced_in",
                                     "reason": "рост меди уже в цене майнеров (+40% за год)"}),
                    ("search_step", {"hyp_id": "h2", "outcome": "reject", "reject_kind": "mechanism_refuted",
                                     "reason": "портфель заказов кабельщика снизился в 3 кв."})]),
        _msg("Два кандидата слабые — сегодня по меди нечего."),           # раньше здесь поиск и кончался
        # код вернул: «поиск не завершён, следующий шаг — направление d1»
        _msg(calls=[("search_add", {"thesis": "Переработчики лома получают маржу от разрыва цен", "ticker": "SCHN",
                                    "mechanism": "разрыв цены лома и катодной меди расширяет маржу переработки",
                                    "circle": 2, "direction_id": "d1"})]),
    ] + [_msg(calls=[("search_step", {"hyp_id": "h3", "outcome": "pass", "data": data})]) for _ in range(6)] + [
        _msg("Нашёл: Schnitzer (SCHN — переработка металлолома)."),
    ]
    llm = FakeLLM(script)
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text, _files, _cost = ag.run("найди идею про медь")
    injected = [m for b in llm.bodies for m in b["messages"]
                if m.get("role") == "user" and "поиск «медь» не завершён" in str(m.get("content"))]
    assert injected, "код не вернул модель к незавершённому поиску"
    s = SQ.active(None, tmp_path) or SQ.all_searches(tmp_path)[0]
    assert s["статус"] == SQ.FOUND and s["итог"]["тикер"] == "SCHN"
    assert "найден достойный кандидат" in text
    assert ag.search_rounds >= 1


def test_agent_turn_end_pauses_search_honestly(tmp_path):
    tools = A.Tools()
    tools.search_dir = tmp_path
    script = [_msg(calls=[("search_start", {"topic": "уран"})]),
              _msg(calls=[("search_add", {"thesis": "Обогатители выигрывают от запрета российского урана",
                                          "ticker": "LEU", "mechanism": "запрет импорта обогащённого урана"})])]
    llm = FakeLLM(script, default="Пока смотрю дальше.")
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text, _f, _c = ag.run("найди идею про уран")
    s = SQ.all_searches(tmp_path)[0]
    assert s["статус"] == SQ.PAUSED and s["пауза"]["вид"] == "turn"
    assert s["бюджет"]["потрачено_usd"] > 0 and len(s["гипотезы"]) == 1      # очередь цела, траты учтены
    assert "Сам в фоне не продолжаю" in text and "найден" not in text.split("Поиск")[-1]
    # следующий ответ «продолжай» — тот же поиск, не новый
    llm2 = FakeLLM([_msg(calls=[("search_start", {"topic": "уран"})])], default="Продолжаю.")
    ag2 = A.Agent(post=llm2, tools=A.Tools(), api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag2.tools.search_dir = tmp_path
    ag2.run("продолжай")
    assert len(SQ.all_searches(tmp_path)) == 1


def test_hunter_honest_zero_is_pause_not_success(tmp_path):
    z = {"checked": 3, "reasons": ["всё уже в цене"],
         "best_candidate": {"name": "Cameco", "ticker": "CCJ", "what_missing": "нет данных о контрактах на 2027",
                            "becomes_idea_if": "контракт ≥10 млн фунтов до 2026-12-31"},
         "branches": [{"what": "LEU (Centrus — обогащение урана) от запрета импорта", "kind": "priced_in",
                       "why_not": "запрет уже в консенсусе выручки 2027 года", "return_if": "продление льгот отменено"},
                      {"what": "UEC (Uranium Energy — добыча в США)", "kind": "mechanism_refuted",
                       "why_not": "добыча не растёт: лицензии отложены до 2028", "return_if": "лицензия выдана"}]}
    p = tmp_path / "idea_2026-11-09_none.json"
    p.write_text(json.dumps(z, ensure_ascii=False), encoding="utf-8")
    s = SQ.from_hunt(none_path=p, topic="уран", date="2026-11-09", d=tmp_path / "q")
    assert s["статус"] == SQ.PAUSED and s["пауза"]["вид"] == "executor"   # честный ноль ≠ успех
    kinds = {h["тикер"]: (h["вердикт"], (h["отказ"] or {}).get("вид")) for h in s["гипотезы"]}
    assert kinds["LEU"] == (SQ.REJECTED, "priced_in") and kinds["UEC"] == (SQ.REJECTED, "mechanism_refuted")
    assert kinds["CCJ"][0] == SQ.NEEDS_DATA
    # следующий запуск по той же теме продолжает тот же поиск и не повторяет отвергнутое
    s2 = SQ.from_hunt(none_path=p, topic="уран", date="2026-11-10", d=tmp_path / "q")
    assert s2["id"] == s["id"] and len(s2["гипотезы"]) == 3
