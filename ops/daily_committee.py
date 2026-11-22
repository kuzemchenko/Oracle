# -*- coding: utf-8 -*-
"""ops/daily_committee.py — ЕЖЕДНЕВНАЯ САМОСТОЯТЕЛЬНАЯ РАБОТА НАД ИДЕЯМИ (Q-ежедневный-комитет).

Владелец 22.11: «чтобы работа велась каждый день, и если идея оценивается относительно высоко, то она углубляется,
и если проходит некий порог — ты мне сразу пишешь экстренное сообщение». Его числа: бюджет $15 в день, экстренное
сообщение при балле комитета 7 и выше (после проверки независимым судьёй), остальное — одной строкой в утреннем
«что покопать».

Что делает один запуск (бот — раз в день после «что покопать»; всё остальное время владельца не трогает):
  1) берёт темы сегодняшнего «что покопать», которые владелец не взял сам («Копаем») и не отклонил («Мимо»);
  2) по каждой — поиск тем же агентом, что в Telegram (сбор доказательств — дешёвая модель, критик и судья — модели
     двух других разработчиков), сначала глубина: одна лучшая гипотеза до полного вердикта комитета;
  3) балл и решение ставит КОД (ops/idea_verdict.py); вердикт записывается ДО исхода (счёт метода по группам балла);
  4) балл ≥ порога и судья доводы проверил → экстренное сообщение владельцу (текст пишет код) — ЗА ФЛАГОМ
     daily_committee_push; ниже порога — строка в следующем утреннем «что покопать».

Каждый поиск — в своей папке reports/ideas/searches_daily/<id>/: очередь владельца в Telegram не затрагивается.
Разговор агента — journal/daily_committee_chat.jsonl (замер точки Б считает только разговоры владельца). В боевой
журнал прогнозов (П16) самостоятельные идеи не пишутся: их счёт — по записям вердиктов комитета.
Деньги: дневной потолок — config/limits.yaml per_run_token_budget_usd.daily_committee (слово владельца), месячный
лимит проверяется до старта (orchestrator/run_budget) и НЕ меняется; траты — настоящие (journal/costs.jsonl).

  python3 ops/daily_committee.py run [--mode live] [--max-ideas N] [--budget X] [--no-push]
  python3 ops/daily_committee.py show          → итог последнего дня и строка для утреннего сообщения
"""
import argparse
import datetime
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

OUT = ROOT / "reports" / "daily_committee"
SEARCH_ROOT = ROOT / "reports" / "ideas" / "searches_daily"
CHAT = ROOT / "journal" / "daily_committee_chat.jsonl"
NOTICES = ROOT / "journal" / "notices.jsonl"
DIG_DIR = ROOT / "reports" / "dig"
DIG_DECISIONS = ROOT / "journal" / "dig_decisions.jsonl"
RULES = ROOT / "knowledge" / "idea_committee.yaml"
BUDGET_MODE = "daily_committee"
DEFAULTS = {"порог_сообщения": 7, "бюджет_идеи_usd": 4.0, "ходов_на_идею": 4, "идей_в_день": 4}
OVERSHOOT_RESERVE = 0.4      # запас дня: 22.11 день закрылся на $15.23 при потолке $15 (ход доигрывается после потолка)
DIG_MAX_AGE_H = 36
MAX_LEN = 3900
FRAME = "Это исследовательский инструмент, не инвестиционная рекомендация."
FINAL_PAUSES = ("budget", "queue_empty", "month")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def settings(path=None):
    d = yaml.safe_load(pathlib.Path(path or RULES).read_text(encoding="utf-8")) or {}
    return {**DEFAULTS, **{k: v for k, v in (d.get("ежедневно") or {}).items() if k in DEFAULTS}}


def day_budget(limits=None):
    """Дневной потолок — из config/limits.yaml (слово владельца). Нет ключа → 0: работа не начинается."""
    if limits is None:
        from mathlib import limits as L
        limits = L.load_limits()
    return float(((limits or {}).get("per_run_token_budget_usd") or {}).get(BUDGET_MODE) or 0.0)


def _read(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(path):
    out = []
    try:
        for ln in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def day_file(day, out_dir=None):
    return pathlib.Path(out_dir or OUT) / f"{day}.json"


def load_day(day, out_dir=None):
    return _read(day_file(day, out_dir)) or {"дата": str(day), "идеи": [], "потрачено_usd": 0.0}


def save_day(rec, out_dir=None):
    p = day_file(rec["дата"], out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return p


# ── что копать ──────────────────────────────────────────────────────────────────────────────────────
def topics(now=None, dig_dir=None, decisions=None, done=()):
    """Темы сегодняшнего «что покопать», свободные для самостоятельной работы: владелец их не взял сам и не
    отклонил, и сегодня по ним ещё не работали. → [{'тема', 'запрос'}]"""
    now = now or _now()
    files = sorted(pathlib.Path(dig_dir or DIG_DIR).glob("dig_*.json"))
    if not files:
        return []
    pr = _read(files[-1]) or {}
    try:
        age_h = (now - datetime.datetime.fromisoformat(str(pr.get("ts")))).total_seconds() / 3600
    except (TypeError, ValueError):
        return []
    if age_h > DIG_MAX_AGE_H or age_h < 0:
        return []
    taken = {str(r.get("тема")) for r in _jsonl(decisions or DIG_DECISIONS)
             if r.get("файл") == files[-1].stem and str(r.get("решение")) in ("копаем", "мимо")}
    out = []
    for o in pr.get("предложения") or []:
        t = str(o.get("тема") or "").strip()
        if not t or t in taken or t in done:
            continue
        circles = "; ".join(str(c) for c in (o.get("круги") or [])[:3])
        out.append({"тема": t, "запрос": (
            f"Найди идею по теме «{t}». Что случилось: {o.get('что_случилось')} Почему это может пойти дальше: "
            f"{o.get('почему_дальше')} Круги: {circles} Почему рынок может этого не видеть: "
            f"{o.get('почему_рынок_не_видит')} Что опровергнет: {o.get('что_опровергнет')} "
            "Это самостоятельная работа — владелец сейчас не пишет. Сначала реши СТОРОНУ: посмотри «РЫНОК И СЕКТОРЫ» в "
            "сводке состояния; если сектор корректируется — рассмотри, кто проигрывает от события (идея на падение, "
            "search_add direction 'short'), покупку против сектора бери только с особой причиной. Работай по листу "
            "комитета, сначала глубина: выбери ОДНУ лучшую бумагу и доведи гипотезу до полного вердикта — факты со ссылками, звенья до денег компании, "
            "размер эффекта как ПРИРОСТ из-за события, чего ждёт рынок и наш расчёт того же показателя за тот же период, "
            "сценарии с расчётом цели, событие с датой, critic_check и честные ответы на возражения; замечания "
            "независимого судьи исправляй по существу. Если по существу идея не держится — отклони её с причиной. "
            "Итог пиши коротко.")})
    return out


def owner_topic(name, context=""):
    """Тема, названная владельцем («прогони через комитет …»): тот же порядок работы, что и для тем утреннего меню."""
    return {"тема": str(name).strip(), "запрос": (
        f"Найди идею по теме «{str(name).strip()}». Что известно (проверь сам по первоисточникам, цифры возьми свежие): "
        f"{str(context).strip()} Это самостоятельная работа по просьбе владельца — он сейчас не пишет. Сначала реши "
        "СТОРОНУ: посмотри «РЫНОК И СЕКТОРЫ» в сводке состояния; идея может быть на рост или на падение (search_add "
        "direction 'short'); «сейчас не покупать» — честный ответ. Работай по листу комитета, сначала глубина: выбери "
        "ОДНУ лучшую бумагу и доведи гипотезу до полного вердикта — факты со ссылками, звенья до денег компании, размер "
        "эффекта как ПРИРОСТ из-за события, чего ждёт рынок и наш расчёт того же показателя за тот же период, сценарии "
        "с расчётом цели, событие с датой, critic_check и честные ответы на возражения; замечания независимого судьи "
        "исправляй по существу. Если по существу идея не держится — отклони её с причиной. Итог пиши коротко.")}


def next_prompt(topic):
    return (f"Продолжай поиск по теме «{topic}»: закрой незачтённые пункты листа комитета и замечания независимого "
            "судьи у лучшей гипотезы (committee_verdict покажет какие). Новых гипотез не открывай, пока эта не доведена "
            "или не отклонена по существу. Итог пиши коротко.")


# ── один поиск до вердикта ──────────────────────────────────────────────────────────────────────────
def work_idea(topic, budget, max_turns=4, search_root=None, answer=None, log=print):
    """Тема → поиск в своей папке → лучший вердикт комитета. → запись об идее (деньги — фактические)."""
    import bot_agent as BA
    import idea_verdict as IV
    import search_queue as SQ
    root = pathlib.Path(search_root or SEARCH_ROOT)
    sid = "s_" + _now().strftime("%Y%m%dT%H%M%S%fZ")
    sd = root / sid                                              # свой поиск — своя папка: чужие очереди не видны
    s = SQ.start(topic["тема"], "telegram", budget_usd=budget, d=sd, sid=sid)
    history, spent, turns = [], 0.0, 0
    for i in range(1, max_turns + 1):
        q = topic["запрос"] if i == 1 else next_prompt(topic["тема"])
        tools = BA.Tools()
        tools.search_dir, tools.verdict_dir, tools.pred_path = sd, sd / "verdicts", sd / "predictions.jsonl"
        try:
            if answer is not None:
                text, _f, cost = answer(q, history, tools)
            else:
                text, _f, cost = BA.answer(q, history=history, agent=BA.Agent(tools=tools))
        except Exception as e:                                   # noqa: BLE001 — сбой хода не теряет собранное
            log(f"  ход {i}: сбой — {type(e).__name__}: {str(e)[:160]}")
            break
        turns += 1
        spent += float(cost or 0)
        history += [{"role": "user", "text": q}, {"role": "assistant", "text": text}]
        s = SQ.load(s["id"], sd)
        kind = (s.get("пауза") or {}).get("вид")
        log(f"  ход {i}: ${float(cost or 0):.2f} | поиск: {s['статус']}")
        if s["статус"] in (SQ.FOUND, SQ.CANCELLED) or (s["статус"] == SQ.PAUSED and kind in FINAL_PAUSES) \
                or spent >= budget:
            break
    s = SQ.load(s["id"], sd)
    allv = IV.search_verdicts(s)
    scored = [v for v in allv if v.get("балл") is not None]
    best = scored[0] if scored else None
    rec = {"тема": topic["тема"], "поиск": s["id"], "папка": str(sd.relative_to(ROOT)) if sd.is_relative_to(ROOT) else str(sd),
           "статус_поиска": s["статус"], "ходов": turns, "потрачено_usd": round(spent, 4),
           "гипотез": len(s.get("гипотезы") or []), "отклонено": sum(1 for v in allv if v.get("отклонена")),
           "причины_отказа": [f"{v.get('тикер')}: {v.get('причина')}" for v in allv if v.get("отклонена")][:4]}
    if best:
        h = next((x for x in s["гипотезы"] if x["id"] == best.get("гипотеза")), {})
        rec.update({"тикер": best["тикер"], "имя": best.get("имя"), "балл": best["балл"], "из": best["из"],
                    "решение": best["решение"], "решение_словами": best["решение_словами"],
                    "судья": bool(best.get("судья")), "брак": best.get("брак") or [],
                    "измерено": sum(1 for p in best["пункты"] if p["зачтён"] is not None),
                    "не_хватает": [p["почему"] or p["словами"] for p in best["не_хватает"]][:4],
                    "тезис": str(h.get("тезис") or "")[:500], "вердикт_текст": IV.render(best)})
        IV.save(best)                                            # запись ДО исхода — счёт метода по группам балла
    return rec


# ── тексты (пишет код) ──────────────────────────────────────────────────────────────────────────────
def _label(idea):
    return f"{idea.get('имя')} ({idea.get('тикер')})" if idea.get("имя") else str(idea.get("тикер"))


def urgent_text(idea):
    """Экстренное сообщение владельцу: идея прошла порог и независимого судью. Одно сообщение Telegram."""
    head = (f"Самостоятельная работа: идея набрала {idea['балл']} из {idea['из']} и прошла независимого судью — "
            f"{idea['решение_словами']}.\nТема: {idea['тема']}.\nБумага: {_label(idea)}.\n")
    body = "Суть: " + idea.get("тезис", "") + "\n\n" + idea.get("вердикт_текст", "")
    tail = "\n\nХочешь разобрать — напиши «разбери " + str(idea.get("тикер")) + "». " + FRAME
    room = MAX_LEN - len(head) - len(tail)
    return head + (body if len(body) <= room else body[:room - 1] + "…") + tail


def morning_line(now=None, out_dir=None, threshold=None):
    """Строка для утреннего «что покопать»: что система вчера (≤36 ч назад) проработала сама. Нет работы → None."""
    now = now or _now()
    threshold = threshold if threshold is not None else settings()["порог_сообщения"]
    for back in (0, 1):
        day = (now - datetime.timedelta(days=back)).date().isoformat()
        rec = _read(day_file(day, out_dir))
        if rec and rec.get("идеи"):
            break
    else:
        return None
    parts = []
    for i in rec["идеи"][:4]:
        if i.get("балл") is None:
            why = str((i.get("причины_отказа") or ["до оценки не дошла"])[0]).split(": ")
            # «TICK: вид отказа: длинная причина» → в строке утром — бумага и вид отказа, без обрыва на полуслове
            short = ": ".join(why[:2]) if len(why) >= 2 else why[0][:80]
            n_rej = int(i.get("отклонено") or 0)
            parts.append(f"«{i['тема']}» — отклонена" + (f" (гипотез {n_rej}; первая — {short})" if n_rej else f" ({short})"))
        else:
            miss = ("; не хватило: " + str(i["не_хватает"][0])[:110]) if i.get("не_хватает") and i["балл"] < threshold else ""
            parts.append(f"«{i['тема']}» → {_label(i)}: {i['балл']} из {i['из']}, {i['решение_словами']}{miss}")
    when = "Сегодня" if rec["дата"] == now.date().isoformat() else "Вчера"
    return (f"{when} копал сам (потрачено ${rec.get('потрачено_usd', 0):.2f}): " + " | ".join(parts)
            + ". Балл — число проверок, пройденных с доказательством, не вероятность роста.")


def notify(text, path=None):
    """Сообщение владельцу через канал заметок бота (journal/notices.jsonl — бот шлёт несекретные заметки сам)."""
    p = pathlib.Path(path or NOTICES)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now().isoformat(timespec="seconds"), "text": text,
                             "source": "ежедневная работа комитета"}, ensure_ascii=False) + "\n")


def _push_on():
    try:
        import bot_reports as R
        return bool(R.load_presentation().get("daily_committee_push"))
    except Exception:                                            # noqa: BLE001 — нет конфига = не шлём
        return False


# ── запуск дня ──────────────────────────────────────────────────────────────────────────────────────
def run(now=None, max_ideas=None, budget=None, push=None, out_dir=None, search_root=None, dig_dir=None,
        decisions=None, answer=None, precheck=True, notices=None, cfg=None, log=print, extra_topics=None):
    import bot_agent as BA
    now = now or _now()
    cfg = cfg or settings()
    day = now.date().isoformat()
    rec = load_day(day, out_dir)
    cap = float(budget if budget is not None else day_budget())
    left = cap - float(rec.get("потрачено_usd") or 0)
    rec["бюджет_usd"] = cap
    if cap <= 0:
        log("дневной бюджет не задан (config/limits.yaml per_run_token_budget_usd.daily_committee) — не работаю")
        return rec
    if precheck and answer is None:
        from orchestrator import run_budget as RB
        dec = RB.precheck(BUDGET_MODE, expected_calls=40)
        if not dec.get("allowed"):
            rec["отказ_бюджета"] = dec.get("reason")
            log("бюджет: " + str(dec.get("reason")))
            save_day(rec, out_dir)
            return rec
    BA.CHAT_LOG, BA.CHAT_REQUESTS = CHAT if out_dir is None else pathlib.Path(out_dir) / "chat.jsonl", \
        (CHAT.parent / "daily_committee_requests.jsonl") if out_dir is None else pathlib.Path(out_dir) / "requests.jsonl"
    done = {i["тема"] for i in rec["идеи"]}
    if extra_topics is not None:                     # темы владельца — вместо утреннего меню, в том же дневном бюджете
        todo = [x for x in extra_topics if x["тема"] not in done]
        n_max = len(rec["идеи"]) + len(todo)
    else:
        todo = topics(now, dig_dir, decisions, done=done)
        n_max = int(max_ideas if max_ideas is not None else cfg["идей_в_день"])
    do_push = _push_on() if push is None else bool(push)
    for t in todo[:max(0, n_max - len(rec["идеи"]))]:
        per = min(float(cfg["бюджет_идеи_usd"]), left - OVERSHOOT_RESERVE)   # последний ход может выйти за потолок поиска
        if per < 1.0:
            log(f"остаток дневного бюджета ${left:.2f} — на новую идею не хватает")
            break
        log(f"тема: {t['тема']} (до ${per:.2f})")
        idea = work_idea(t, per, int(cfg["ходов_на_идею"]), search_root, answer, log)
        left -= idea["потрачено_usd"]
        rec["потрачено_usd"] = round(float(rec.get("потрачено_usd") or 0) + idea["потрачено_usd"], 4)
        if idea.get("балл") is not None and idea["балл"] >= int(cfg["порог_сообщения"]) and idea.get("судья"):
            idea["экстренное_сообщение"] = urgent_text(idea)
            idea["отправлено"] = False
            if do_push:
                notify(idea["экстренное_сообщение"], notices)
                idea["отправлено"] = True
        rec["идеи"].append(idea)
        save_day(rec, out_dir)                                   # после каждой идеи — сбой дальше не теряет сделанное
        log(f"  итог: {_label(idea) if idea.get('тикер') else 'до оценки не дошла'} — "
            f"{idea.get('балл', '—')} из 10, ${idea['потрачено_usd']:.2f}")
    if not todo:
        log("свободных тем на сегодня нет (меню пустое, устарело или все темы взяты владельцем)")
    save_day(rec, out_dir)
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ежедневная самостоятельная работа комитета над идеями")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--mode", default="live", choices=("live",))
    p.add_argument("--max-ideas", type=int, default=None)
    p.add_argument("--budget", type=float, default=None)
    p.add_argument("--no-push", action="store_true")
    p = sub.add_parser("topic", help="тема владельца: прогнать через комитет в счёт дневного бюджета")
    p.add_argument("--name", action="append", required=True, help="тема (можно несколько раз)")
    p.add_argument("--context", action="append", default=None, help="что известно по теме (по одному на тему)")
    p.add_argument("--no-push", action="store_true")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    if a.cmd == "topic":
        ctx = a.context or []
        extra = [owner_topic(n, ctx[i] if i < len(ctx) else (ctx[0] if ctx else "")) for i, n in enumerate(a.name)]
        rec = run(extra_topics=extra, push=False if a.no_push else None, log=lambda *x: print(*x, flush=True))
        print(f"за день потрачено ${rec.get('потрачено_usd', 0):.2f} из ${rec.get('бюджет_usd', 0):.2f}; идей: {len(rec['идеи'])}")
        return 0
    if a.cmd == "run":
        rec = run(max_ideas=a.max_ideas, budget=a.budget, push=False if a.no_push else None,
                  log=lambda *x: print(*x, flush=True))
        print(f"за день потрачено ${rec.get('потрачено_usd', 0):.2f} из ${rec.get('бюджет_usd', 0):.2f}; идей: {len(rec['идеи'])}")
    else:
        files = sorted(OUT.glob("20*.json"))
        if not files:
            print("самостоятельной работы ещё не было")
            return 0
        rec = _read(files[-1]) or {}
        print(json.dumps({k: v for k, v in rec.items() if k != "идеи"}, ensure_ascii=False))
        for i in rec.get("идеи") or []:
            print("\n— " + i["тема"] + f" (${i['потрачено_usd']:.2f}, ходов {i['ходов']})")
            print(i.get("вердикт_текст") or "до оценки не дошла: " + "; ".join(i.get("причины_отказа") or ["нет гипотез"]))
            if i.get("экстренное_сообщение"):
                print("\n[экстренное сообщение — " + ("отправлено" if i.get("отправлено") else "НЕ отправлено (флаг выключен)") + "]\n"
                      + i["экстренное_сообщение"])
        print("\n[строка для утреннего сообщения]\n" + str(morning_line()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
