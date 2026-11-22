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
    llm = FakeLLM(script, default="Пока смотрю дальше, вернусь с результатом.")
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text, _f, _c = ag.run("найди идею про уран")
    assert "Поправка кода" in text                                          # обещание фона поправлено кодом
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


def test_find_idea_without_search_start_is_started_by_code(tmp_path):
    """14.11: агент отвечал на «найди идею» без search_start — код начинает поиск сам и возвращает модель к очереди."""
    tools = A.Tools()
    tools.search_dir = tmp_path
    llm = FakeLLM([_msg("Вот пара мыслей про медь, но защищать нечего.")], default="Смотрю дальше.")
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag.run("Найди идею по теме «медь и стройка»")
    ss = SQ.all_searches(tmp_path)
    assert len(ss) == 1 and "медь" in ss[0]["тема"]
    injected = [m for b in llm.bodies for m in b["messages"]
                if m.get("role") == "user" and "не завершён" in str(m.get("content"))]
    assert injected


def test_plain_question_does_not_start_search(tmp_path):
    tools = A.Tools()
    tools.search_dir = tmp_path
    ag = A.Agent(post=FakeLLM([_msg("Золото дорогое.")]), tools=tools, api_key="x",
                 log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag.run("что думаешь про золото?")
    assert SQ.all_searches(tmp_path) == []


def test_status_question_does_not_charge_paused_search(tmp_path):
    """14.11: разговор, который лишь спросил статус поиска, не списывается в его бюджет."""
    s = SQ.start("нефть", "telegram", d=tmp_path)
    SQ.pause(s, "budget", "бюджет исчерпан")
    SQ.save(s, tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    llm = FakeLLM([_msg(calls=[("search_status", {})], cost=0.7), _msg("Поиск стоит на паузе.")])
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag.run("почему поиск остановлен?")
    assert SQ.load(s["id"], tmp_path)["бюджет"]["потрачено_usd"] == 0.0


# ── ревью 14.11: новая тема не подмешивается к старому поиску ─────────────────────────────────────
def test_topic_similarity_is_code_not_llm():
    assert SQ.topic_differs("найди идею про уран", "медь и стройка")
    assert not SQ.topic_differs("найди идею про медь", "Найди идею по теме «медь и стройка»")
    assert not SQ.topic_differs("что с ценами меди", "медь")                    # форма слова — та же тема
    assert not SQ.topic_differs("продолжай", "медь")                             # сравнивать нечего — продолжаем
    assert not SQ.topic_differs("найди идею", "уран")
    assert SQ.topic_similarity("продолжай", "медь") is None


def test_new_topic_pauses_old_search_and_starts_new(tmp_path):
    t1 = A.Tools()
    t1.search_dir = tmp_path
    t1.search_start("медь и стройка")
    t1.search_add("Переработчики лома получают маржу от разрыва цен", "SCHN", "разрыв цены лома и катодной меди")
    old_id = t1.search_id
    t2 = A.Tools()                                   # следующий ответ владельцу — другая тема
    t2.search_dir = tmp_path
    out = json.loads(t2.search_start("обогащение урана"))
    assert t2.search_id != old_id and "новую тему" in out["сообщение"]
    old = SQ.load(old_id, tmp_path)
    assert old["статус"] == SQ.PAUSED and old["пауза"]["вид"] == "new_topic"
    assert "владелец начал новую тему: обогащение урана" in old["пауза"]["причина"]
    assert len(old["гипотезы"]) == 1                                             # ничего не удалено
    new = SQ.load(t2.search_id, tmp_path)
    assert new["статус"] == SQ.RUNNING and new["гипотезы"] == [] and "урана" in new["тема"]
    assert SQ.active("telegram", tmp_path)["id"] == new["id"]                    # текущий — новый, не отложенный
    assert "отложен" in SQ.status_line(old).lower()
    # возврат к прежней теме — продолжается отложенный поиск, третий не создаётся
    t3 = A.Tools()
    t3.search_dir = tmp_path
    t3.search_start("вернёмся к меди")
    assert t3.search_id == old_id and len(SQ.all_searches(tmp_path)) == 2
    assert SQ.load(old_id, tmp_path)["статус"] == SQ.RUNNING
    assert SQ.is_set_aside(SQ.load(new["id"], tmp_path))


def test_same_topic_continues_and_budget_pause_survives_set_aside(tmp_path):
    t1 = A.Tools()
    t1.search_dir = tmp_path
    t1.search_start("медь и стройка")
    sid = t1.search_id
    for topic in ("медь", "найди идею про медь", "продолжай"):                   # та же/неясная тема — тот же поиск
        t = A.Tools()
        t.search_dir = tmp_path
        t.search_start(topic)
        assert t.search_id == sid
    assert len(SQ.all_searches(tmp_path)) == 1
    # в пределах одного ответа переформулировка темы моделью поиск не меняет
    t1.search_start("редкоземельные металлы")
    assert t1.search_id == sid and len(SQ.all_searches(tmp_path)) == 1
    # пауза по бюджету не затирается пометкой «отложен»: вид и причина целы
    s = SQ.load(sid, tmp_path)
    s["бюджет"]["потрачено_usd"] = s["бюджет"]["лимит_usd"]
    SQ.pause(s, "budget", "бюджет поиска исчерпан")
    SQ.save(s, tmp_path)
    t4 = A.Tools()
    t4.search_dir = tmp_path
    t4.search_start("обогащение урана")
    s = SQ.load(sid, tmp_path)
    assert s["пауза"]["вид"] == "budget" and "новую тему" in s["пауза"]["отложен"]
    assert SQ.resume(s)[1]                                                       # бюджетный запрет на продолжение цел


def test_find_idea_on_new_topic_started_by_code_despite_old_search(tmp_path):
    SQ.start("медь и стройка", "telegram", d=tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    llm = FakeLLM([_msg("Пара мыслей про уран.")], default="Смотрю дальше.")
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag.run("Найди идею про обогащение урана")
    ss = {s["тема"]: s for s in SQ.all_searches(tmp_path)}
    assert len(ss) == 2 and SQ.is_set_aside(ss["медь и стройка"])



def test_depth_first_takes_the_most_advanced_hypothesis(tmp_path):
    """Владелец 22.11 «сначала глубина»: гипотеза, прошедшая быструю проверку, ведётся до конца раньше остальных."""
    s = SQ.start("уран", "telegram", d=tmp_path)
    h1, _ = SQ.add_hypothesis(s, "Обогатители выигрывают от запрета российского урана", "LEU", "запрет импорта обогащённого урана")
    h2, _ = SQ.add_hypothesis(s, "Конвертеры урана получают премию за мощность", "CCJ", "дефицит конверсии UF6 поднимает цену")
    assert SQ.next_action(s)["гипотеза"] == "h1" and SQ.depth_block(s) is None      # обе в начале — первая
    h2["этап"] = "углубление"
    nxt = SQ.next_action(s)
    assert nxt["гипотеза"] == "h2" and "Сначала глубина" in nxt["текст"]
    assert "h2 CCJ" in SQ.depth_block(s)
    h2["вердикт"] = SQ.NEEDS_DATA                                                   # ждёт данных — не мешает
    assert SQ.depth_block(s) is None and SQ.next_action(s)["гипотеза"] == "h1"


def test_agent_cannot_add_hypotheses_while_one_is_deep(tmp_path):
    s = SQ.start("уран", "telegram", d=tmp_path)
    h1, _ = SQ.add_hypothesis(s, "Обогатители выигрывают от запрета российского урана", "LEU", "запрет импорта обогащённого урана")
    h1["этап"] = "критика"
    SQ.save(s, tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    out = json.loads(tools.search_add("Конвертеры урана получают премию за мощность", "CCJ",
                                      "дефицит конверсии UF6 поднимает цену"))
    assert "сначала глубина" in out["сообщение"] and len(SQ.load(s["id"], tmp_path)["гипотезы"]) == 1



def test_numbers_written_with_words_are_read_as_numbers():
    """Прогон DHT 22.11: «32% (пояснение)» и «субъективно ~45%: …» — это числа, пункт не должен падать из-за формы."""
    assert SQ.lead_number("32% (риск-нейтрально, по ценам опционов до экспирации 16.04.2027)") == 32.0
    assert SQ.lead_number("субъективно ~45%: Q4-прибыль ~$2.6/акцию уже законтрактована") == 45.0
    assert SQ.lead_number("$3,34") == 3.34 and SQ.lead_number(7) == 7.0
    assert SQ.lead_number("рост ожиданий с $1.87 до $3.34") is None        # число не в начале — не выдёргиваем
    assert SQ.lead_number(True) is None and SQ.lead_number(None) is None
    d = SQ.unwrap({"market_view": {"kind": "magnitude", "market": {"value": "32% (опционы)", "source": "EODHD"},
                                   "ours": {"value": "субъективно ~45%: бронь"}}})
    assert d["market_view"]["market"]["value"] == 32.0 and d["market_view"]["ours"]["value"] == 45.0
    assert d["market_view"]["market"]["value_text"] == "32% (опционы)"       # слова модели сохранены


def test_code_fills_materiality_numbers_from_its_own_calculation(tmp_path):
    """Прогон DHT 22.11: расчёт был (materiality_check), но в данные попал итог словами — числа дописывает код."""
    tools = A.Tools()
    tools.search_dir = tmp_path
    assert tools._fill_materiality({"materiality": {"вердикт": "СУЩЕСТВЕННО"}}) == {"materiality": {"вердикт": "СУЩЕСТВЕННО"}}
    tools.materiality_check(effect_profit_per_year=420e6, net_income=160e6, market_cap=3470e6, years=1)
    out = tools._fill_materiality({"materiality": {"вердикт": "СУЩЕСТВЕННО", "детали": "11% капитализации"}})
    mt = out["materiality"]
    assert mt["effect_profit_per_year"] == 420e6 and mt["market_cap"] == 3470e6 and mt["детали"] == "11% капитализации"
    own = {"materiality": {"effect_profit_per_year": 1.0, "net_income": 2.0}}
    assert tools._fill_materiality(own) == own                              # свои числа модели код не трогает


# ── находки проверочного прогона 22.11 (DHT): записанное видно, добытое записывается, критика внутри поиска ──
def _deep_search(tmp_path):
    s = SQ.start("танкеры", "telegram", d=tmp_path)
    h, _ = SQ.add_hypothesis(s, "Танкерная компания недооценена после падения ставок фрахта", "DHT",
                             "законтрактованные рейсы четвёртого квартала дают прибыль выше ожиданий")
    h["этап"] = "критика"
    h["данные"] = {"факты": [{"текст": "Отчёт за 3 кв.: рекордная выручка", "url": "https://example.com/q3"}],
                   "цена": 21.58, "market_view": {"kind": "magnitude", "metric": "прибыль на акцию 2027",
                                                  "market": {"value": 3.34, "source": "консенсус"}, "ours": {"value": 4.0}}}
    SQ.save(s, tmp_path)
    return s, h


def test_status_shows_what_is_already_recorded(tmp_path):
    s, h = _deep_search(tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    out = json.loads(tools.search_status())
    rec = out["уже_записано"]["h1"]
    assert rec["цена"] == "21.58" and "example.com/q3" in rec["факты"][0] and rec["market_view"]["market"] == 3.34
    assert "НЕ добывай его заново" in out["важно"]
    assert "уже_записано" not in json.loads(tools._sq_view(s))             # после каждого шага сводку не раздуваем


def test_critic_objections_are_recorded_by_code_and_only_answers_come_from_author(tmp_path):
    s, h = _deep_search(tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    assert "не запускается" in tools.critic_check("h1")                    # без модели-критика — честный отказ
    asked = []

    def critic(brief):
        asked.append(brief)
        return {"семейство": "openai", "модель": "openai/gpt-x", "возражения": [
            {"текст": "Законтрактованная прибыль уже известна рынку из отчёта за третий квартал", "существенное": True},
            {"текст": "Мультипликатор цели взят с пика цикла", "существенное": False}]}

    tools.critic_fn = critic
    out = json.loads(tools.critic_check("h1"))
    assert [o["n"] for o in out["возражения"]] == [1, 2] and "example.com/q3" in asked[0]
    cr = SQ.load(s["id"], tmp_path)["гипотезы"][0]["данные"]["критика"]
    assert cr["семейство"] == "openai" and cr["записал"] == "код" and cr["возражения"][0]["ответ"] == ""
    assert "уже записана" in tools.critic_check("h1") and len(asked) == 1  # второй раз деньги не тратим
    # автор пытается переписать возражение и снять «существенное» — код берёт только ответ
    tools.search_step("h1", "pass", data={"критика": {"семейство": "anthropic", "возражения": [
        {"n": 1, "текст": "мелочь", "существенное": False, "ответ": "Нет: цена упала на 18% уже после отчёта, бронь выросла"}]}})
    cr = SQ.load(s["id"], tmp_path)["гипотезы"][0]["данные"]["критика"]
    assert cr["семейство"] == "openai" and cr["возражения"][0]["существенное"] is True
    assert cr["возражения"][0]["текст"].startswith("Законтрактованная") and cr["возражения"][0]["ответ"].startswith("Нет:")
    tools.search_step("h1", "pass", data={"критика": {"ответы": {"2": "Согласен, цель предварительная — P/E 6.5 допущение"}}})
    cr = SQ.load(s["id"], tmp_path)["гипотезы"][0]["данные"]["критика"]
    assert cr["возражения"][1]["ответ"].startswith("Согласен") and cr["возражения"][0]["ответ"].startswith("Нет:")


def test_agent_critic_is_another_family_and_its_cost_counts(tmp_path):
    seen = []

    def post(body):
        seen.append(body)
        return {"choices": [{"message": {"content": 'Вот: {"возражения":[{"текст":"Ставки фрахта могут упасть до нормы за две недели","существенное":true}]}'}}],
                "usage": {"cost": 0.2}}

    ag = A.Agent(post=post, tools=A.Tools(), api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    res = ag._critic("идея")
    assert res["семейство"] != "anthropic" and res["возражения"][0]["существенное"] is True
    assert not seen[0]["model"].startswith("anthropic/") and "tools" not in seen[0]
    assert ag.cost == 0.2 and ag.tools.critic_fn == ag._critic


def test_save_reserve_forces_recording_before_budget_runs_out(tmp_path, monkeypatch):
    """Три ответа прогона DHT ($7.35) не записали ничего: теперь с 65% потолка ответа добытое обязано быть записано."""
    monkeypatch.setattr(A, "MAX_TURN_COST_USD", 1.0)
    s, h = _deep_search(tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    tools.eodhd = lambda **k: "цены"
    script = [_msg(calls=[("search_start", {"topic": "танкеры"})], cost=0.1),
              _msg(calls=[("eodhd", {"endpoint": "eod/DHT.US"})], cost=0.3),
              _msg(calls=[("eodhd", {"endpoint": "eod/DHT.US"})], cost=0.3),          # 0.7 ≥ 65% → режим записи
              _msg(calls=[("search_step", {"hyp_id": "h1", "outcome": "pass", "data": {
                  "durability": {"reason": "контракты на год вперёд уже подписаны", "url": "https://example.com/c"}}})], cost=0.1),
              _msg("Записал.", cost=0.05)]
    llm = FakeLLM(script)
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    ag._loop([{"role": "user", "content": "продолжай"}], 10)
    names = [[t["function"]["name"] for t in b["tools"]] for b in llm.bodies]
    assert "eodhd" in names[2] and set(names[3]) <= set(A.SAVE_TOOLS)       # до записи — только инструменты поиска
    assert "eodhd" in names[4]                                              # записал — инструменты вернулись
    nudge = [m for m in llm.bodies[3]["messages"] if "в гипотезу НЕ записано" in str(m.get("content"))]
    assert nudge and "2 обращений к данным" in str(nudge[0]["content"])
    assert SQ.load(s["id"], tmp_path)["гипотезы"][0]["данные"]["durability"]["url"] == "https://example.com/c"
    assert ag.unsaved == 0 and ag.save_phase is False


def test_final_text_is_not_accepted_while_research_is_unsaved(tmp_path):
    """Повторный прогон DHT: модель добыла данные и сразу написала длинный итог, ничего не записав."""
    s, h = _deep_search(tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    tools.eodhd = lambda **k: "цены"
    script = [_msg(calls=[("search_start", {"topic": "танкеры"})]),
              _msg(calls=[("eodhd", {"endpoint": "eod/DHT.US"})]),
              _msg("Длинный разбор без записи."),
              _msg(calls=[("search_step", {"hyp_id": "h1", "outcome": "pass", "data": {
                  "precedent": {"what": "2019, санкции против COSCO", "amplitude": "+30%", "url": "https://example.com/p"}}})]),
              _msg("Записал: похожий случай. Не хватает проверки другой моделью.")]
    llm = FakeLLM(script)
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text = ag._loop([{"role": "user", "content": "продолжай"}], 10)
    assert text.startswith("Записал") and len(llm.bodies) == 5
    assert any("итог не принят" in str(m.get("content")) for m in llm.bodies[3]["messages"])
    assert {t["function"]["name"] for t in llm.bodies[3]["tools"]} <= set(A.SAVE_TOOLS)
    assert SQ.load(s["id"], tmp_path)["гипотезы"][0]["данные"]["precedent"]["amplitude"] == "+30%"


def test_plain_answer_without_search_is_not_held_back(tmp_path):
    tools = A.Tools()
    tools.search_dir = tmp_path
    tools.eodhd = lambda **k: "цены"
    llm = FakeLLM([_msg(calls=[("eodhd", {"endpoint": "eod/DHT.US"})]), _msg("Цена 21.58.")])
    ag = A.Agent(post=llm, tools=tools, api_key="x", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    assert ag._loop([{"role": "user", "content": "сколько стоит DHT?"}], 10) == "Цена 21.58." and len(llm.bodies) == 2


# ── сторона ставки (владелец 22.11: «рынок падал — почему не предлагаешь шортить?») ──
def test_hypothesis_side_is_recorded_and_sector_against_it_is_said_at_once(tmp_path, monkeypatch):
    import sector_view as SV
    seen = []

    def for_ticker(ticker, direction="вверх", **k):
        seen.append((ticker, direction))
        return {"против": direction == "вверх", "словами": "сектор «чипы» (фонд SMH) — корректируется: ниже своей средней на 24%"}

    import importlib
    for name in ("sector_view", "ops.sector_view"):                 # у модуля два имени — подменяем оба
        monkeypatch.setattr(importlib.import_module(name), "for_ticker", for_ticker)
    s = SQ.start("чипы", "telegram", d=tmp_path)
    tools = A.Tools()
    tools.search_dir = tmp_path
    out = json.loads(tools.search_add("Производитель чипов выигрывает от нового заказа", "NVDA", "заказ поднимает выручку"))
    assert "сторона: на рост" in out["сообщение"] and "для покупки сектор против" in out["сообщение"]
    assert "кто ПРОИГРЫВАЕТ" in out["сообщение"]
    out = json.loads(tools.search_add("Сборщик серверов теряет заказы из-за налога на импорт", "SMCI",
                                      "налог режет маржу сборки серверов", direction="short"))
    assert "сторона: на падение" in out["сообщение"] and "ВНИМАНИЕ" not in out["сообщение"]
    hs = SQ.load(s["id"], tmp_path)["гипотезы"]
    assert [h["данные"]["направление"] for h in hs] == ["long", "short"] and seen == [("NVDA", "вверх"), ("SMCI", "вниз")]


def test_queue_steps_ask_for_losers_too(tmp_path):
    s = SQ.start("налог ЕС", "telegram", d=tmp_path)
    assert "проигравшие (идеи на падение)" in SQ.next_action(s)["текст"]
    assert "проигравший" in SQ.STAGE_TODO["генерация"]
    assert "ПРОИГРЫВАЕТ" in A.SYSTEM_PROMPT and "не покупать" in A.SYSTEM_PROMPT
