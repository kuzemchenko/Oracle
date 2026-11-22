# -*- coding: utf-8 -*-
"""ops/committee_probe.py — ПРОВЕРОЧНЫЙ ПРОГОН КОМИТЕТА: одну бумагу довести до глубины тем же агентом, что в Telegram.

Владелец 22.11: «как убедиться, что фильтр не слишком жёсткий?» → «Да, проверь». Вопрос прогона — проходим ли лист
комитета: наберёт ли идея, по которой собраны доказательства по всем 10 пунктам, хотя бы 6 (лист ожидания).
Замер 22.11: из 10 оценённых идей глубоко не проверена ни одна — судить о строгости листа было не по чему.

Прогон идёт В ОТДЕЛЬНОЙ ПАПКЕ (reports/committee_tests/<дата>_<ТИКЕР>/): своя очередь поиска, свои записи вердиктов,
свой журнал разговора и свой файл прогнозов. Разговоры владельца в Telegram, журнал печатей (П16), замер точки Б и
счёт метода комитета не затрагиваются; владельцу ничего не отправляется. Траты — настоящие (journal/costs.jsonl),
потолок — бюджет поиска (--budget, по умолчанию $10).

  python3 ops/committee_probe.py DHT --name "DHT Holdings — нефтяные танкеры" \
      --card reports/ideas/idea_2026-11-03_DHT.card.json [--budget 10] [--max-turns 6]
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

FINAL_PAUSES = ("budget", "queue_empty", "month")


def first_prompt(ticker, name, card):
    src = (f" Это идея охотника — карточка {card} (прочитай её; тезис обнови на сегодня: что изменилось с тех пор, "
           f"цены возьми свежие)." if card else "")
    return (f"Найди идею — проверочный прогон по ОДНОЙ бумаге: {name} ({ticker}).{src} Заведи одну гипотезу по "
            f"{ticker} и доведи её до полного вердикта комитета: собери доказательства по всем 10 пунктам листа — "
            f"звенья от события до денег компании со ссылками (chain), эффект для прибыли (materiality), почему эффект "
            f"продержится (durability), чего ждёт рынок и чем мы отличаемся (market_view с числом и источником), "
            f"похожий случай с числом хода цены (precedent), сценарии с ценами и расчётом цели, событие с датой "
            f"(катализатор), проверку моделью другого разработчика (critic_check → ответы на возражения). "
            f"Вызывай committee_verdict, чтобы видеть, какие пункты ещё не зачтены, и закрывай именно их. Новых гипотез "
            f"не открывай. Если по существу идея не держится — отклони её честно с причиной.")


def next_prompt(ticker):
    return (f"Продолжай поиск по {ticker}: закрой незачтённые пункты листа комитета у этой же гипотезы "
            f"(committee_verdict покажет какие). Новых гипотез не открывай.")


def models_with(model):
    """Настройки моделей с другой моделью у роли агента — ТОЛЬКО для прогона (config/models.yaml не меняется, агент
    владельца в Telegram остаётся на своей). Запасных моделей нет: тихого возврата к дорогой не будет."""
    import copy
    from orchestrator import openrouter as OR
    import bot_agent as BA
    models = copy.deepcopy(OR.load_models())
    for sect in ("roles", "school_roles"):
        if BA.ROLE in (models.get(sect) or {}):
            models[sect][BA.ROLE] = {**models[sect][BA.ROLE], "primary": model, "fallbacks": [],
                                     "family": model.split("/", 1)[0]}
            return models
    raise KeyError(f"роль {BA.ROLE} не найдена в config/models.yaml")


def run(ticker, name, card=None, budget=10.0, max_turns=6, out=None, answer=None, log=print, model=None):
    import bot_agent as BA
    import idea_verdict as IV
    import search_queue as SQ
    from mathlib import idea_score as IS
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    out = pathlib.Path(out or ROOT / "reports" / "committee_tests" / f"{today}_{ticker}")
    sd, vd = out / "searches", out / "verdicts"
    sd.mkdir(parents=True, exist_ok=True)
    BA.CHAT_LOG, BA.CHAT_REQUESTS = out / "chat_agent.jsonl", out / "chat_requests.jsonl"
    s = SQ.active("telegram", sd) or SQ.start(f"Проверочный прогон комитета: {name} ({ticker})", "telegram",
                                              budget_usd=budget, d=sd)
    history, spent, turns = [], 0.0, []
    tr = out / "transcript.md"
    for i in range(1, max_turns + 1):
        q = first_prompt(ticker, name, card) if i == 1 else next_prompt(ticker)
        tools = BA.Tools()
        tools.search_dir, tools.verdict_dir, tools.pred_path = sd, vd, out / "predictions.jsonl"
        try:
            if answer is not None:
                text, _files, cost = answer(q, history, tools)
            else:
                ag = BA.Agent(tools=tools, models=models_with(model)) if model else BA.Agent(tools=tools)
                text, _files, cost = BA.answer(q, history=history, agent=ag)
        except Exception as e:                                   # noqa: BLE001 — сбой хода не теряет собранное
            log(f"ход {i}: сбой — {type(e).__name__}: {str(e)[:200]}")
            turns.append({"ход": i, "сбой": f"{type(e).__name__}: {str(e)[:200]}"})
            break
        spent += float(cost or 0)
        history += [{"role": "user", "text": q}, {"role": "assistant", "text": text}]
        with tr.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n## Ход {i} — ${float(cost or 0):.2f}\n\n**Вопрос:** {q}\n\n**Ответ:**\n\n{text}\n")
        s = SQ.load(s["id"], sd)
        vs = [v for v in IV.search_verdicts(s) if v.get("балл") is not None]
        best = vs[0] if vs else None
        measured = sum(1 for p in best["пункты"] if p["зачтён"] is not None) if best else 0
        turns.append({"ход": i, "стоимость": round(float(cost or 0), 4), "статус": s["статус"],
                      "балл": best["балл"] if best else None, "измерено": measured})
        log(f"ход {i}: ${float(cost or 0):.2f} (всего ${spent:.2f}) | поиск: {s['статус']} | "
            f"балл {best['балл'] if best else '—'} из 10, измерено пунктов {measured}")
        kind = (s.get("пауза") or {}).get("вид")
        if s["статус"] == SQ.FOUND or (s["статус"] == SQ.PAUSED and kind in FINAL_PAUSES) or spent >= budget:
            break
    s = SQ.load(s["id"], sd)
    allv = IV.search_verdicts(s)
    scored = [v for v in allv if v.get("балл") is not None]
    rules = IV.load_rules()
    res = {"дата": today, "тикер": ticker, "модель": model or "как в Telegram", "потрачено_usd": round(spent, 4), "ходов": len(turns), "ходы": turns,
           "статус_поиска": s["статус"], "вердикты": allv, "строгость": IS.strictness(scored, rules)}
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    text = IV.render_search(allv, s.get("тема")) + "".join("\n\n" + IV.render(v) for v in scored[:1])
    (out / "verdict.txt").write_text(text + "\n", encoding="utf-8")
    log("\n" + text)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Проверочный прогон комитета: одна бумага до глубины, в отдельной папке")
    ap.add_argument("ticker")
    ap.add_argument("--name", required=True)
    ap.add_argument("--card", default=None)
    ap.add_argument("--budget", type=float, default=10.0)
    ap.add_argument("--max-turns", type=int, default=6)
    ap.add_argument("--out", default=None, help="папка прогона (по умолчанию reports/committee_tests/<дата>_<ТИКЕР>)")
    ap.add_argument("--model", default=None, help="модель агента только для прогона (например anthropic/claude-sonnet-4.6)")
    a = ap.parse_args(argv)
    run(a.ticker.upper(), a.name, a.card, a.budget, a.max_turns, out=a.out, model=a.model,
        log=lambda *x: print(*x, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
