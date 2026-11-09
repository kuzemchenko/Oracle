# -*- coding: utf-8 -*-
"""ops/demo_find_idea.py — демонстрация пути «найди идею» после VQ-4 (задание владельца 09.11).

Гоняет НАСТОЯЩИЙ код агента Telegram (ops/bot_agent.Agent), очереди поиска (ops/search_queue), прокачки
(orchestrator/deep_dive) и статуса идеи (ops/idea_revision). Подставная только модель: её ходы заданы
сценарием, чтобы показать, что делает КОД — без трат и без сети. Всё пишется во временную папку;
боевые журналы и файлы не трогаются.

  python3 ops/demo_find_idea.py [--out reports/review_2026-11-09/VQ-4_демо.md]
"""
import argparse
import datetime
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

TODAY = datetime.date.today().isoformat()


def _msg(content=None, calls=None):
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = [{"id": f"c{i}", "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                           for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": m, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"cost": 0.02}}


class ScriptedModel:
    def __init__(self, script, default="Готово."):
        self.script, self.default, self.bodies = list(script), default, []

    def __call__(self, body):
        self.bodies.append(body)
        return self.script.pop(0) if self.script else _msg(self.default)


def _worthy():
    return {
        "факты": [{"текст": "Контракт на поставку лома меди на $400 млн до 2028 г. (8-K)", "url": "https://www.sec.gov/demo-8k",
                   "опубликовано": "2026-11-05", "проверено": TODAY},
                  {"текст": "Консенсус выручки 2027 — $2.1 млрд, 9 аналитиков (EODHD Earnings::Trend)",
                   "url": "https://eodhd.com/api/fundamentals/DEMO.US", "опубликовано": "2026-11-08", "проверено": TODAY}],
        "механизм_проверен": "контракт добавляет ~19% к выручке 2027 года сверх консенсуса",
        "цена": 20.0, "дата_цены": TODAY, "потенциал_пп": 25, "риски": ["срыв поставок"], "инструмент_доступен": True,
        "market_view": {"kind": "magnitude", "metric": "выручка 2027, $ млрд",
                        "market": {"value": 2.1, "source": "консенсус EODHD Earnings::Trend"}, "ours": {"value": 2.5},
                        "why_different": "все 9 оценок консенсуса датированы до публикации контракта 05.11",
                        "основано_на": [1, 2]},
        "сценарии": {"цена": 20.0, "дата_цены": TODAY, "источник_цены": "снимок EODHD close",
                     "базовый": {"цена": 25.0, "основание": "P/S 2.0 × выручка $2.5 млрд / 200 млн акций"},
                     "неблагоприятный": {"цена": 16.0, "основание": "контракт сорван, P/S сжимается до 1.6"}},
        "направление": "long", "горизонт": "2027-03-31", "условия_реализации": ["отчёт за 4 кв. подтверждает отгрузки"],
        "условия_опровержения": ["выручка 4 кв. ниже $480 млн", "расторжение контракта до 2026-12-31"],
        "критика": {"семейство": "openai", "возражения": [{"текст": "контракт может быть отложен", "существенное": True,
                                                           "ответ": "график поставок зафиксирован в 8-K, штраф за перенос — у покупателя"}]},
    }


def _transcript(model):
    """Что видела модель от кода: вызовы инструментов и вмешательства кода (без системной подсказки)."""
    rows = []
    last = model.bodies[-1]["messages"] if model.bodies else []
    for m in last[1:]:
        if m.get("role") == "user":
            c = m.get("content")
            c = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            rows.append(("КОД → модели" if c.startswith("[система]") else "Владелец", c))
        elif m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                rows.append(("Модель вызывает", f"{fn['name']}({fn['arguments'][:160]}…)"))
            if m.get("content") and not m.get("tool_calls"):
                rows.append(("Модель пишет", str(m["content"])[:300]))
        elif m.get("role") == "tool":
            c = m.get("content")
            if isinstance(c, list):
                c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            try:
                d = json.loads(c)
                rows.append(("Ответ кода", f"{d.get('сообщение') or ''} | статус: {d.get('статус_ru')} | "
                                           f"следующий шаг: {(d.get('следующий_шаг') or {}).get('текст', '')[:160]}"))
            except (ValueError, TypeError):
                rows.append(("Ответ кода", str(c)[:200]))
    return rows


def run(out=None):
    import bot_agent as A
    from ops import search_queue as SQ
    from ops import idea_revision as IR
    from orchestrator import deep_dive as DD
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vq4_demo_"))
    SQ.SEARCH_DIR = tmp / "searches"
    IR.REV_DIR = tmp / "revisions"
    A.CHAT_LOG, A.CHAT_REQUESTS = tmp / "chat.jsonl", tmp / "req.jsonl"
    L = [f"# VQ-4: путь «найди идею» — демонстрация ({TODAY})", "",
         "Что это значит для продукта: ниже — что делает КОД, когда модель пытается закончить поиск на первых "
         "отказах, когда кончается ресурс и когда прокачка опровергает идею. Модель подставная (ходы заданы "
         "сценарием), всё остальное — боевой код; боевые файлы не трогаются.", ""]

    # ── 1. два отказа → третий кандидат → найден ──
    data = _worthy()
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
        _msg("Оба кандидата слабые — сегодня по меди нечего."),
        _msg(calls=[("search_add", {"thesis": "Переработчики лома получают маржу от разрыва цен", "ticker": "DEMO",
                                    "mechanism": "разрыв цены лома и катодной меди расширяет маржу переработки",
                                    "circle": 2, "direction_id": "d1"})]),
        _msg(calls=[("search_step", {"hyp_id": "h3", "outcome": "pass", "data": {}})]),
        _msg(calls=[("search_step", {"hyp_id": "h3", "outcome": "pass", "data": {"цена": 20.0}})]),   # быстрая проверка без фактов
    ] + [_msg(calls=[("search_step", {"hyp_id": "h3", "outcome": "pass", "data": data})]) for _ in range(5)] + [
        _msg("Нашёл кандидата: DEMO (переработка металлолома) — разбор ниже."),
    ]
    model = ScriptedModel(script)
    tools = A.Tools()
    tools.search_dir = SQ.SEARCH_DIR
    ag = A.Agent(post=model, tools=tools, api_key="demo", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text, _f, cost = ag.run("найди идею про медь")
    L += ["## 1. Первые два кандидата отклонены → поиск переходит к третьему", ""]
    L += [f"- **{who}:** {what}" for who, what in _transcript(model)]
    L += ["", "Итог, ушедший владельцу (последний абзац пишет код):", "", "```", text, "```", ""]

    # ── 2. ресурс ответа кончился → пауза, очередь цела, успех не засчитан ──
    script2 = [_msg(calls=[("search_start", {"topic": "уран"})]),
               _msg(calls=[("search_add", {"thesis": "Обогатители выигрывают от запрета российского урана",
                                           "ticker": "LEU", "mechanism": "запрет импорта обогащённого урана"})])]
    model2 = ScriptedModel(script2, default="Пока смотрю дальше, вернусь с результатом.")
    tools2 = A.Tools()
    tools2.search_dir = SQ.SEARCH_DIR
    ag2 = A.Agent(post=model2, tools=tools2, api_key="demo", log_cost=lambda *a, **k: None, sleep=lambda s: None)
    text2, _f, _c = ag2.run("найди идею про уран")
    s2 = SQ.load(tools2.search_id, SQ.SEARCH_DIR)
    L += ["## 2. Ресурс ответа кончился → пауза с причиной, очередь сохранена, успех не засчитан", "",
          f"- статус поиска: **{SQ.STATUS_RU[s2['статус']]}**, вид паузы: {s2['пауза']['вид']}; "
          f"гипотез в очереди: {len(s2['гипотезы'])}; потрачено: ${s2['бюджет']['потрачено_usd']:.2f}",
          f"- следующий шаг сохранён: {SQ.next_action({**s2, 'статус': SQ.RUNNING})['текст']}", "",
          "Конец ответа владельцу:", "", "```", text2[-600:], "```", ""]
    s2b = SQ.load(tools2.search_id, SQ.SEARCH_DIR)
    SQ.resume(s2b)                       # владелец написал «продолжай» — и в этом ответе бюджет кончился
    SQ.charge(s2b, 10)
    SQ.save(s2b, SQ.SEARCH_DIR)
    L += ["Если в следующем ответе кончится бюджет самого поиска (суб-потолок $5):", "", "```", SQ.status_line(SQ.load(s2b["id"], SQ.SEARCH_DIR)), "```", ""]

    # ── 3. прокачка: день 1 опроверг → плана нет, статус «отклонена», поиск снова не выполнен ──
    ideas = tmp / "ideas"
    ideas.mkdir()
    (ideas / "idea_2026-11-02_MP.card.json").write_text(
        (ROOT / "reports" / "ideas" / "idea_2026-11-02_MP.card.json").read_text("utf-8"), encoding="utf-8")
    sh = SQ.start("редкие земли", "hunter")
    h, _ = SQ.add_hypothesis(sh, "MP Materials выигрывает от запрета экспорта из Китая", "MP",
                             "запрет экспорта редкоземельных магнитов поднимает цены")
    h["вердикт"], sh["статус"], sh["итог"] = SQ.WORTHY, SQ.FOUND, {"тикер": "MP", "когда": TODAY}
    SQ.save(sh)
    q = tmp / "deep_dive.jsonl"
    q.write_text(json.dumps({"ts": "2026-11-07T10:00:00+00:00", "run_id": "h_2026-11-02_MP", "asset": "MP.US",
                             "status": "queued"}, ensure_ascii=False) + "\n", encoding="utf-8")
    real_ch = json.loads((ROOT / "journal" / "challenges" / "challenge_20261107T140624Z_983986_1.json").read_text("utf-8"))
    orig = IR.closes_from_snapshot
    IR.closes_from_snapshot = lambda *a, **k: [("2026-11-06", 40.0)]
    r = DD.run_deep_dive(now=datetime.datetime(2026, 11, 7, 14, 6, tzinfo=datetime.timezone.utc), queue_path=q,
                         notices_path=tmp / "notices.jsonl", passports_dir=tmp / "pass", ideas_dir=ideas,
                         challenge_fn=lambda a, candidate=None: real_ch)
    IR.closes_from_snapshot = orig
    note = [json.loads(x)["text"] for x in (tmp / "notices.jsonl").read_text("utf-8").splitlines()]
    cur = IR.current(str(ideas / "idea_2026-11-02_MP"), closes=[("2026-11-06", 40.0)], today="2026-11-07")
    L += ["## 3. День 1 прокачки опроверг идею → день 2 не выдаёт план покупки", "",
          "Разбор — настоящий протокол суда по MP от 07.11 (итог «РАЗБИТА»). Прежде 08.11 после него ушёл паспорт с входом.",
          f"- сообщений владельцу: {len(note)}; дней 2 (паспортов): {r['день2']}; закрыто: {r.get('закрыто')}",
          f"- актуальный статус идеи: **{cur['статус']}**, план действует: {cur['план_действует']}",
          f"- поиск «редкие земли»: {SQ.status_line(SQ.load(sh['id']))}", "", "```", note[0], "```", ""]

    # ── 4. стоп пробит / катализатор прошёл ──
    card = json.loads((ideas / "idea_2026-11-02_MP.card.json").read_text("utf-8"))
    stem2 = tmp / "ideas2"
    stem2.mkdir()
    (stem2 / "idea_2026-11-02_MP.card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    c_stop = IR.current(str(stem2 / "idea_2026-11-02_MP"), closes=IR.closes_from_snapshot("MP.US") or
                        [("2026-11-05", 33.74)], today="2026-11-09", out_dir=tmp / "rev4")
    c_cat = IR.current(str(stem2 / "idea_2026-11-02_MP"), closes=[("2026-11-10", 40.0), ("2026-11-11", 40.5)],
                       today="2026-11-12", out_dir=tmp / "rev4")
    L += ["## 4. Цена пересекла условие отмены / катализатор состоялся", "",
          f"- MP по снимку цен сервера: **{c_stop['статус']}** — {'; '.join(c_stop['основания'])}",
          *[f"  {x}" for x in IR.plan_or_archive(card, c_stop, ['Вход. Не дороже $40.30'])],
          f"- если бы цена держалась, а катализатор 10.11 прошёл: **{c_cat['статус']}** — вопрос: {c_cat['вопросы'][0]}",
          "", "## 5. Исходные прогнозы не меняются",
          f"- проверка отпечатков исходных карточек: {IR.verify() or 'исходные карточки записанных идей не менялись'}", ""]
    md = "\n".join(L)
    if out:
        p = ROOT / out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
    return md


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    print(run(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
