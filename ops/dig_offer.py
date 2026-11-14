# -*- coding: utf-8 -*-
"""ops/dig_offer.py — ежедневное «что покопать» (владелец 14.11, очередь Q-поиск-по-расписанию).

Владелец: «смотреть каждый день на большие события и тренды, искать те, по которым есть потенциал
неочевидных каскадных движений, предлагать мне покопать с человеческим обоснованием, и если я согласен —
смотреть глубже».

Поток: меню событий (ops/event_menu.py, сервер 21:00 UTC) + датчики толпы (reports/attention/) →
ОДИН вызов модели: отобрать 0–3 повода, где волна может дойти дальше очевидного, и объяснить по-человечески
→ КОД проверяет: каждый повод — только из пронумерованных пунктов меню (П8: событие не выдумывается,
ссылки берёт код), обязательные поля, тип «почему рынок не видит», без повторов недавних тем, без
эмодзи/жаргона/голых тикеров → код дописывает похожий исторический каскад из библиотеки
(ops/analogs.py, knowledge/cascade_history.yaml) → протокол reports/dig/dig_<время>.json → бот шлёт
одно сообщение с кнопками «Копаем N» / «Мимо» (ops/bot.py _tick_dig_offer). «Копаем» = запрос
владельца «найди идею» по теме: агент Telegram ведёт поиск (ops/search_queue.py, потолок $10).

Это НЕ прогноз и не суждение о рынке (ничего не печатается, П16 не касается); это карта, где искать.
«Сегодня копать нечего» — легитимный итог.

  python3 ops/dig_offer.py run [--mode live|mock|auto]   — собрать предложения → reports/dig/
  python3 ops/dig_offer.py show [<файл>]                 — показать текст сообщения (последний протокол)
"""
import argparse
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

OUT_DIR = ROOT / "reports" / "dig"
MENU = ROOT / "reports" / "events" / "menu_latest.json"
ATT_DAILY = ROOT / "reports" / "attention" / "attention_latest.json"
ATT_FAST = ROOT / "reports" / "attention" / "fast_latest.json"
DECISIONS = ROOT / "journal" / "dig_decisions.jsonl"
SEARCH_DIR = ROOT / "reports" / "ideas" / "searches"
PROMPT = ROOT / "agents" / "prompts" / "dig_scout.md"
ROLE = "dig_scout"
BUDGET_MODE = "dig_offer"

MAX_OFFERS = 3
MENU_MAX_AGE_H = 36          # меню старше — честно «данных нет», не предлагаем по вчерашнему
REPEAT_DAYS = 14             # тему, предложенную/отклонённую за 14 дней, не повторяем
TYPES = ("дата", "вынужденный продавец", "подписанные деньги", "правило", "другое")
TYPE_RU = {"дата": "рынок недооценивает дату", "вынужденный продавец": "кто-то вынужден продавать",
           "подписанные деньги": "деньги уже подписаны, но не в цене", "правило": "правило понято неверно",
           "другое": "другое"}
FIELDS = ("тема", "что_случилось", "почему_дальше", "почему_рынок_не_видит", "что_опровергнет")
FIELD_MAX = 260
MAX_LEN = 4000               # одно сообщение Telegram (≤4096); не влезает — код убирает последнюю ветку
FRAME = "Это исследовательский инструмент, не инвестиционная рекомендация."
_WORD = re.compile(r"[a-zа-яё0-9]{4,}", re.IGNORECASE)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _read_json(p):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(p):
    out = []
    try:
        for line in pathlib.Path(p).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


# ── входы ────────────────────────────────────────────────────────────────────────────────
def menu_items(menu, attention_daily=None, attention_fast=None):
    """Пронумерованные поводы с источниками. id: N — крупные события NewsAPI, G — резкий рост в мировых
    новостях (GDELT), W — резкий рост просмотров Википедии, A — внимание толпы к бумаге (Википедия по дням),
    F — быстрые следы толпы (новости/опционы/объём за часы). Ссылки берёт КОД — модель их не пишет."""
    items = []
    m = menu or {}
    for i, e in enumerate(((m.get("newsapi") or {}).get("события") or [])[:15], 1):
        uri = e.get("uri")
        items.append({"id": f"N{i}", "текст": f"{e.get('событие')} — {e.get('статей')} статей, {e.get('дата')}",
                      "url": f"https://eventregistry.org/event/{uri}" if uri else None})
    for i, e in enumerate(((m.get("gdelt") or {}).get("резко_выросло") or [])[:15], 1):
        ex = e.get("примеры") or []
        items.append({"id": f"G{i}", "текст": (f"в мировых новостях: {e.get('что')} — {e.get('статей_сутки')} статей "
                                              f"за сутки, в {e.get('рост_раз')} раза больше прошлых суток; "
                                              f"примеры: {'; '.join(ex[:3])}"),
                      "url": ex[0] if ex else None})
    for i, e in enumerate(((m.get("wiki") or {}).get("резко_выросло") or [])[:15], 1):
        t = str(e.get("статья") or "")
        items.append({"id": f"W{i}", "текст": (f"Википедия: «{t}» — {e.get('просмотров_вчера')} просмотров вчера, "
                                              f"в {e.get('рост_раз')} раза больше позавчера"),
                      "url": "https://en.wikipedia.org/wiki/" + t.replace(" ", "_") if t else None})
    for i, s in enumerate(((attention_daily or {}).get("сигналы") or [])[:10], 1):
        pg = s.get("страница")
        items.append({"id": f"A{i}", "текст": (f"внимание к бумаге {s.get('бумага')} (страница «{pg}»): фаза "
                                              f"«{s.get('фаза')}», просмотров в {s.get('ускорение')} раза выше "
                                              f"своей нормы, {s.get('дней_выше_нормы')} дней подряд"),
                      "url": f"https://en.wikipedia.org/wiki/{pg}" if pg else None})
    for i, s in enumerate(((attention_fast or {}).get("сигналы") or [])[:10], 1):
        parts = []
        for k in ("новости", "википедия", "объём", "опционы"):
            v = s.get(k) or {}
            if v.get("ok") and v.get("кратно") is not None:
                parts.append(f"{k} ×{v.get('кратно')} к норме")
        items.append({"id": f"F{i}", "текст": f"быстрые следы толпы по {s.get('бумага')}: {', '.join(parts) or 'нет чисел'}",
                      "url": None})
    return [it for it in items if it.get("текст")]


def recent_topics(now=None, out_dir=None, decisions=None, search_dir=None):
    """Темы, которые уже предлагали/отклоняли/искали за REPEAT_DAYS дней (не повторяем без новых данных)."""
    now = now or _now()
    since = (now - datetime.timedelta(days=REPEAT_DAYS)).isoformat()
    out = []
    d = pathlib.Path(out_dir or OUT_DIR)
    if d.exists():
        for f in sorted(d.glob("dig_*.json")):
            pr = _read_json(f) or {}
            if str(pr.get("ts") or "") >= since:
                out += [str(o.get("тема")) for o in pr.get("предложения") or []]
    for r in _jsonl(decisions or DECISIONS):
        if str(r.get("ts") or "") >= since and r.get("тема"):
            out.append(str(r["тема"]))
    sd = pathlib.Path(search_dir or SEARCH_DIR)
    if sd.exists():
        for f in sorted(sd.glob("s_*.json")):
            s = _read_json(f) or {}
            if str(s.get("обновлён") or s.get("создан") or "") >= since and s.get("тема"):
                out.append(str(s["тема"]))
    return list(dict.fromkeys(t for t in out if t and t != "None"))


def _words(t):
    return {w.lower() for w in _WORD.findall(str(t or ""))}


def is_repeat(topic, recent, thr=0.5):
    a = _words(topic)
    if not a:
        return False
    for r in recent:
        b = _words(r)
        if b and len(a & b) / min(len(a), len(b)) >= thr:
            return r
    return False


# ── запрос и разбор ответа ───────────────────────────────────────────────────────────────
def library(episodes=None):
    """Библиотека исторических каскадов (knowledge/cascade_history.yaml) — {id: эпизод}."""
    if episodes is None:
        try:
            import analogs as AN
            episodes = AN.load()
        except Exception:                               # noqa: BLE001 — без библиотеки аналога честно нет
            episodes = []
    return {str(e.get("id")): e for e in episodes if e.get("id")}


def build_user_prompt(items, recent, now, lib=None):
    lines = [f"Сегодня {now.date().isoformat()}. Поводы дня (берёшь ТОЛЬКО отсюда, ссылаешься по id):"]
    lines += [f"[{it['id']}] {it['текст']}" for it in items]
    lines.append("\nНедавно уже предлагали или искали (не повторять без нового факта):")
    lines += [f"- {t}" for t in recent] or ["- ничего"]
    if lib:
        lines.append("\nБиблиотека исторических случаев (для поля «аналог_id» — только id отсюда или null):")
        lines += [f"{k}: {e.get('title_ru')} ({str(e.get('trigger_date') or '')[:4]})" for k, e in lib.items()]
    lines.append(f"\nВерни JSON по формату из инструкции: не больше {MAX_OFFERS} предложений, или пустой список "
                 "с причиной, если копать нечего.")
    return "\n".join(lines)


def parse_response(text):
    if isinstance(text, dict):
        return text
    s = str(text or "").strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def validate_offer(o, ids, recent):
    """→ (offer, None) или (None, причина брака). Код — последнее слово (П8)."""
    if not isinstance(o, dict):
        return None, "не объект"
    for k in FIELDS:
        v = str(o.get(k) or "").strip()
        if not v:
            return None, f"нет поля «{k}»"
        if len(v) > FIELD_MAX:
            o[k] = v[:FIELD_MAX].rsplit(" ", 1)[0] + "…"
    pid = [str(x).strip() for x in (o.get("повод_ids") or []) if str(x).strip()]
    bad = [x for x in pid if x not in ids]
    if not pid:
        return None, "повод не указан — событие без ссылки на меню не предлагаем (П8)"
    if bad:
        return None, f"повод {', '.join(bad)} не из меню дня — выдуманное событие не предлагаем (П8)"
    typ = str(o.get("тип") or "").strip().lower()
    if typ not in TYPES:
        return None, f"тип «{typ}» не из списка — нет ответа «почему рынок этого не видит»"
    circles = [str(c).strip() for c in (o.get("круги") or []) if str(c).strip()][:3]
    if len(circles) < 2:
        return None, "меньше двух кругов — это не каскад, а сама новость"
    rep = is_repeat(o.get("тема"), recent)
    if rep:
        return None, f"повтор недавней темы «{rep}»"
    o["повод_ids"], o["тип"], o["круги"] = pid, typ, [c[:FIELD_MAX] for c in circles]
    return o, None


def attach_sources(o, by_id):
    o["источники"] = [{"id": i, "url": by_id[i].get("url"), "текст": by_id[i]["текст"][:200]}
                      for i in o["повод_ids"] if by_id.get(i)]
    return o


def _short(s, n=200):
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    cut = s[:n]
    dot = cut.rfind(". ")
    return cut[:dot + 1] if dot > n // 2 else cut.rsplit(" ", 1)[0] + "…"


def attach_analog(o, lib):
    """Аналог выбирает модель ПО ID из библиотеки; дату, описание, провальный случай и ссылку ставит КОД
    из knowledge/cascade_history.yaml (П8). id не из библиотеки — аналога нет (не брак ветки)."""
    aid = str(o.get("аналог_id") or "").strip()
    e = (lib or {}).get(aid)
    if not e:
        o["аналог"] = None
        if aid and aid not in ("null", "None"):
            o["аналог_брак"] = f"id «{aid}» не из библиотеки"
        return o
    o["аналог"] = {"id": aid, "дата": e.get("trigger_date"), "что": e.get("title_ru"),
                   "провал": _short(e.get("counter_case_ru")),
                   "url": (e.get("sources") or [{}])[0].get("url")}
    return o


# ── текст владельцу ──────────────────────────────────────────────────────────────────────
def render(pr):
    offers = pr.get("предложения") or []
    if not offers:
        why = pr.get("почему_ничего") or pr.get("ОТКАЗ") or "сильных поводов не нашёл"
        return (f"Что покопать сегодня: ничего не предлагаю.\nПочему: {why}\n\n"
                "Если у тебя есть своя тема — напиши «найди идею про …».\n\n" + FRAME)
    out = ["Что покопать сегодня. Я посмотрел крупные события и тренды за сутки и выбрал поводы, где волна "
           "может дойти дальше очевидного. Это пока не идеи, а места, где стоит искать."]
    for n, o in enumerate(offers, 1):
        out.append(f"\n{n}. {o['тема']}")
        out.append(f"Что случилось: {o['что_случилось']}")
        out.append("Куда может пойти волна: " + " → ".join(o["круги"]))
        out.append(f"Почему дальше очевидного: {o['почему_дальше']}")
        out.append(f"Почему рынок может не видеть ({TYPE_RU.get(o['тип'], o['тип'])}): {o['почему_рынок_не_видит']}")
        a = o.get("аналог")
        if a:
            out.append(f"Как было в истории: «{a['что']}» ({str(a['дата'])[:4]}). Где похожая цепочка ломалась: "
                       f"«{a['провал']}»")
        else:
            out.append("Как было в истории: похожего случая в нашей библиотеке нет — опираемся на логику.")
        out.append(f"Что нас опровергнет: {o['что_опровергнет']}")
        urls = [s["url"] for s in o.get("источники") or [] if s.get("url")]
        if urls:
            out.append("Источник: " + urls[0])
    out.append("\nНажми «Копаем N» — начну поиск идеи по этой теме (до $10 на поиск). «Мимо» — запомню и похожее "
               "без нового факта не предложу.\n\n" + FRAME)
    return "\n".join(out)


def lint(text):
    import presentation_lint as PL
    viol = PL.lint_for_user(text, max_len=MAX_LEN)
    bare = PL._bare_tickers(text)
    if bare:
        viol.append("тикеры без названия компании: " + ", ".join(bare))
    return viol


# ── прогон ───────────────────────────────────────────────────────────────────────────────
def _write(pr, out_dir=None):
    d = pathlib.Path(out_dir or OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    p = d / (pr["run_id"] + ".json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(pr, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def run(mode="auto", now=None, client=None, menu=None, attention_daily=None, attention_fast=None,
        out_dir=None, decisions=None, search_dir=None, episodes=None, budget=True):
    now = now or _now()
    run_id = "dig_" + now.strftime("%Y%m%dT%H%M%SZ")
    pr = {"run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
          "предложения": [], "брак": [], "поводов": 0}
    menu = menu if menu is not None else _read_json(MENU)
    att_d = attention_daily if attention_daily is not None else _read_json(ATT_DAILY)
    att_f = attention_fast if attention_fast is not None else _read_json(ATT_FAST)
    try:
        age_h = (now - datetime.datetime.fromisoformat(str((menu or {}).get("собрано")))).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = None
    if not menu or age_h is None or age_h > MENU_MAX_AGE_H:
        pr["ОТКАЗ"] = ("меню событий не собрано или старше полутора суток — по вчерашним новостям не предлагаю "
                       "(сбор меню сервером в 21:00 UTC)")
        pr["текст"] = render(pr)
        return _write(pr, out_dir), pr
    items = menu_items(menu, att_d, att_f)
    by_id = {it["id"]: it for it in items}
    pr["поводов"] = len(items)
    pr["меню_собрано"] = menu.get("собрано")
    recent = recent_topics(now, out_dir, decisions, search_dir)
    lib = library(episodes)
    pr["недавние_темы"] = recent
    if client is None:
        from orchestrator.openrouter import make_client
        client = make_client(mode=mode, run_id=run_id)
    pr["mode"] = getattr(client, "mode", mode)
    from orchestrator import run_budget as RB
    if budget and pr["mode"] == "live":
        dec = RB.precheck(BUDGET_MODE)
        if not dec.get("allowed"):
            pr["ОТКАЗ"] = f"бюджет: {dec.get('reason')}"
            pr["текст"] = render(pr)
            return _write(pr, out_dir), pr
        client.cost_guard = RB.RunBudgetGuard(BUDGET_MODE, dec["cap_usd"])
    try:
        resp = client.complete(ROLE, PROMPT.read_text(encoding="utf-8"), build_user_prompt(items, recent, now, lib),
                               agent_id="dig_scout", output_kind="dig_offer")
    except RB.RunBudgetExceeded as e:
        pr["ОТКАЗ"] = f"остановлено бюджетом на лету: потрачено ${e.spent_usd:.2f} при потолке ${e.cap_usd}"
        pr["текст"] = render(pr)
        return _write(pr, out_dir), pr
    except Exception as e:                               # noqa: BLE001 — честный отказ
        pr["ОТКАЗ"] = f"модель недоступна: {type(e).__name__}"
        pr["текст"] = render(pr)
        return _write(pr, out_dir), pr
    pr["модель"] = resp.get("model") if isinstance(resp, dict) else None
    pr["стоимость_usd"] = resp.get("cost") if isinstance(resp, dict) else None
    data = parse_response(resp.get("text") if isinstance(resp, dict) else resp)
    if not isinstance(data, dict):
        pr["ОТКАЗ"] = "ответ модели не разобран"
        pr["текст"] = render(pr)
        return _write(pr, out_dir), pr
    pr["почему_ничего"] = str(data.get("почему_ничего") or "")[:FIELD_MAX] or None
    pr["отсеяно_моделью"] = data.get("отсеяно") or []
    for o in (data.get("предложения") or [])[:MAX_OFFERS * 2]:
        ok, why = validate_offer(o, by_id, recent + [x["тема"] for x in pr["предложения"]])
        if ok is None:
            pr["брак"].append({"тема": (o or {}).get("тема") if isinstance(o, dict) else str(o)[:80], "причина": why})
            continue
        attach_analog(attach_sources(ok, by_id), lib)
        pr["предложения"].append(ok)
        if len(pr["предложения"]) >= MAX_OFFERS:
            break
    text = render(pr)
    while len(pr["предложения"]) > 1 and len(text) > MAX_LEN:     # одно сообщение: последняя ветка — за борт
        cut = pr["предложения"].pop()
        pr["брак"].append({"тема": cut.get("тема"), "причина": "не влезла в одно сообщение"})
        text = render(pr)
    viol = lint(text)
    if viol:                                    # жаргон/эмодзи/голые тикеры — не шлём, видно в протоколе
        pr["линт"] = viol
        pr["текст"] = None
    else:
        pr["текст"] = text
    return _write(pr, out_dir), pr


def latest(out_dir=None):
    d = pathlib.Path(out_dir or OUT_DIR)
    fs = sorted(d.glob("dig_*.json")) if d.exists() else []
    return fs[-1] if fs else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", default="auto", choices=("live", "mock", "auto"))
    s = sub.add_parser("show")
    s.add_argument("file", nargs="?")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        p, pr = run(mode=a.mode)
        print(f"{p}: предложений {len(pr['предложения'])}, брак {len(pr['брак'])}"
              + (f", ОТКАЗ: {pr['ОТКАЗ']}" if pr.get("ОТКАЗ") else "")
              + (f", ЛИНТ: {pr['линт']}" if pr.get("линт") else ""))
        return 0
    f = pathlib.Path(a.file) if a.file else latest()
    if not f:
        print("протоколов нет")
        return 1
    pr = _read_json(f) or {}
    print(pr.get("текст") or render(pr))
    if pr.get("линт"):
        print("\n[линт]", pr["линт"])
    if pr.get("брак"):
        print("\n[брак]", json.dumps(pr["брак"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
