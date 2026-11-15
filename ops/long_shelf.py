# -*- coding: utf-8 -*-
"""ops/long_shelf.py — ПОЛКА ДЛИННЫХ ТЕМ (Q-полка-длинных-тем; ревью 14.11 Fable п.3; тезисы владельца T1/T2).

Идеи на 6–24 месяца («литография при буме вычислений») не ищутся каждый раз заново — они лежат на полке
(knowledge/long_themes.yaml), и раз в неделю код по каждой бумаге считает:
  • просадку от максимума за 52 недели (EODHD eod) — «вход на просадке»;
  • оценку против собственной истории: P/E (или P/S, если прибыли нет) — перцентиль за 5 лет (ops/multiples_view);
  • пересмотр прогноза прибыли следующего года аналитиками за 90 дней (EODHD Earnings::Trend → mathlib/expectations);
  • дату следующего отчёта — после него проверка тезиса (mathlib/shelf_rules.next_review).
Статус ставит mathlib/shelf_rules (Инв#6). LLM нет. Снимок — reports/long_shelf/<дата>.json (+ latest.json):
датированная запись ДО исхода — её потом можно сверить форвардом.

  python3 ops/long_shelf.py show      → посчитать и показать сводку (ничего не пишет)
  python3 ops/long_shelf.py tick      → снимок в reports/long_shelf/; сводка владельцу — только при флаге long_shelf_push
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from mathlib import shelf_rules as R          # noqa: E402

THEMES = ROOT / "knowledge" / "long_themes.yaml"
OUT = ROOT / "reports" / "long_shelf"
NOTICES = ROOT / "journal" / "notices.jsonl"
BASE = "https://eodhd.com/api/"
MAX_LEN = 4000
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"]


def _ru(iso):
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
        return f"{d.day} {_MONTHS[d.month - 1]}"
    except ValueError:
        return str(iso)


def load_themes(path=None):
    return yaml.safe_load(pathlib.Path(path or THEMES).read_text(encoding="utf-8")) or {}


# ── сбор (каждый источник — заменяемая функция; сбой одного не роняет полку) ──────────────────────
def _key():
    return os.environ.get("EODHD_API_KEY")


def _get(path, params):
    import requests
    p = dict(params, fmt="json")
    if _key():
        p["api_token"] = _key()
    r = requests.get(BASE + path, params=p, timeout=60)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def fetch_closes(sym, today):
    c, data = _get(f"eod/{sym}", {"from": (today - datetime.timedelta(days=372)).isoformat(), "to": today.isoformat()})
    if c != 200 or not isinstance(data, list):
        return None
    # цена С ПОПРАВКОЙ на сплиты и дивиденды: сырой close давал ложную «просадку 94%» после дробления акций
    return [(str(r["date"])[:10], float(r.get("adjusted_close") or r["close"])) for r in data
            if (r.get("adjusted_close") or r.get("close")) is not None]


def fetch_valuation(sym, today):
    import multiples_view as MV
    v = MV.view(sym, today=today.isoformat())
    if not v.get("ok"):
        return {"причина": v.get("причина") or "нет данных"}
    for k, name in (("pe", "P/E"), ("ps", "P/S")):
        r = v.get(k) or {}
        if r.get("ok"):
            return {"мультипликатор": name, "сейчас": r.get("сейчас"), "медиана": r.get("медиана"),
                    "перцентиль": r.get("перцентиль"), "кварталов": r.get("кварталов")}
    return {"причина": "нет мультипликатора"}


def fetch_revision(sym, today):
    from mathlib import expectations as E
    c, data = _get(f"fundamentals/{sym}", {"filter": "Earnings::Trend"})
    if c != 200 or not data:
        return None
    t = E.parse_earnings_trend(data).get("+1y") or {}
    return t.get("пересмотр_90д")


def fetch_earnings(syms, today):
    c, data = _get("calendar/earnings", {"symbols": ",".join(syms), "from": today.isoformat(),
                                         "to": (today + datetime.timedelta(days=120)).isoformat()})
    rows = (data or {}).get("earnings") if isinstance(data, dict) else data
    out = {}
    for r in rows or []:
        code, d = r.get("code"), r.get("report_date")
        if code and d and (code not in out or d < out[code]):
            out[code] = d
    return out


DEFAULT_FETCH = {"closes": fetch_closes, "valuation": fetch_valuation, "revision": fetch_revision,
                 "earnings": fetch_earnings}


def collect(today=None, themes=None, fetch=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    data = themes or load_themes()
    f = dict(DEFAULT_FETCH, **(fetch or {}))
    rules = data.get("правила") or {}
    syms = [b["бумага"] for t in data.get("темы") or [] for b in t.get("бумаги") or []]
    try:
        earn = f["earnings"](syms, today) or {}
    except Exception:                                              # noqa: BLE001
        earn = {}
    out = {"дата": today.isoformat(), "темы": []}
    for t in data.get("темы") or []:
        rows = []
        for b in t.get("бумаги") or []:
            sym = b["бумага"]
            rec = {"бумага": sym, "имя": b.get("имя"), "роль": b.get("роль")}
            for k in ("closes", "valuation", "revision"):
                try:
                    rec[k] = f[k](sym, today)
                except Exception as e:                             # noqa: BLE001
                    rec[k] = None
                    rec.setdefault("сбои", []).append(f"{k}: {type(e).__name__}")
            dd = R.drawdown(rec.pop("closes") or [])
            val = rec.get("valuation") or {}
            st, why = R.status(dd["просадка_проц"] if dd else None, val.get("перцентиль"), rec.get("revision"), rules)
            entry_px = (round(dd["максимум"] * (1 - abs(float(rules.get("просадка_входа_проц", 20))) / 100), 2)
                        if dd else None)                           # цена, с которой начинается «зона входа»
            rec.update(годовой=dd, статус=st, почему=why, порог_входа=entry_px, отчёт=earn.get(sym) or earn.get(sym.split(".")[0]),
                       пересмотр_тезиса=R.next_review(earn.get(sym) or earn.get(sym.split(".")[0]), today))
            rows.append(rec)
        out["темы"].append({"id": t["id"], "название": t["название"], "тезис": t.get("тезис"),
                            "что_подтвердит": t.get("что_подтвердит"), "что_опровергнет": t.get("что_опровергнет"),
                            "бумаги": rows})
    return out


# ── текст владельцу ──────────────────────────────────────────────────────────────────────────────
def _prev(today, out_dir=None):
    d = pathlib.Path(out_dir or OUT)
    files = sorted(p for p in d.glob("20*.json") if p.stem < today) if d.is_dir() else []
    if not files:
        return {}
    try:
        snap = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {b["бумага"]: b.get("статус") for t in snap.get("темы") or [] for b in t.get("бумаги") or []}


def _bet_line(b):
    dd, val = b.get("годовой") or {}, b.get("valuation") or {}
    bits = []
    if dd:
        bits.append(f"{dd['просадка_проц']:.0f}% от максимума за год" if dd["просадка_проц"] < -1 else "у максимума за год")
    if val.get("перцентиль") is not None:
        pct = round(val["перцентиль"] * 100)
        bits.append(f"оценка дороже, чем в {pct}% кварталов за 5 лет" if pct >= 50 else
                    f"оценка дешевле, чем в {100 - pct}% кварталов за 5 лет")
    rv = b.get("revision")
    if rv is not None:
        bits.append(f"прогноз прибыли подняли на {rv * 100:.0f}%" if rv > 0.005 else
                    f"прогноз прибыли срезали на {abs(rv) * 100:.0f}%" if rv < -0.005 else "прогноз прибыли не меняли")
    if dd and b["статус"] in ("ждать", "дорого") and b.get("порог_входа"):
        bits.append(f"зона входа начнётся при цене ниже ${b['порог_входа']:,.2f} (сейчас ${dd['сейчас']:,.2f})"
                    .replace(",", "\u00a0"))
    label = {"зона входа": "СЕЙЧАС В ЗОНЕ ВХОДА"}.get(b["статус"], b["статус"].upper())
    return f"— {b['имя']} ({b['бумага']}) — {label}: " + "; ".join(bits) + "."


def render(snap, prev=None):
    prev = prev or {}
    allb = [b for t in snap["темы"] for b in t["бумаги"]]
    entry = [b for b in allb if b["статус"] == "зона входа"]
    doubt = [b for b in allb if b["статус"] == "тезис под вопросом"]
    changed = [b for b in allb if prev.get(b["бумага"]) and prev[b["бумага"]] != b["статус"]]
    L = [f"Полка длинных тем — {_ru(snap['дата'])}.", "",
         "Что это значит для тебя. Это идеи на 6–24 месяца, которые мы не ищем заново, а держим и проверяем каждую "
         "неделю. Тезис каждой темы — гипотеза, а не факт. Код смотрит три вещи: насколько цена ниже годового "
         "максимума (вход на просадке), дорого ли компания стоит против своей же истории, и режут ли аналитики "
         "прогнозы прибыли (проверка тезиса). «Сейчас в зоне входа» — на сегодня цена на 20% и больше ниже годового "
         "максимума, а прогнозы не режут; это не сигнал «покупай», а повод разобраться, почему цена упала.", ""]
    if entry:
        L.append("В зоне входа: " + ", ".join(f"{b['имя']} ({b['бумага']})" for b in entry) + ".")
        if len(entry) * 2 >= len(allb):
            L.append(f"Оговорка: в зоне входа сразу {len(entry)} из {len(allb)} бумаг — это общий спад этих отраслей, "
                     "а не столько же отдельных находок. Сначала стоит понять, почему упал весь сектор.")
    if doubt:
        L.append("Тезис под вопросом: " + ", ".join(f"{b['имя']} ({b['бумага']})" for b in doubt) + ".")
    if changed:
        L.append("Изменилось за неделю: " + "; ".join(f"{b['имя']} — было «{prev[b['бумага']]}», стало «{b['статус']}»"
                                                     for b in changed) + ".")
    if not (entry or doubt or changed):
        L.append("За неделю ничего не изменилось: в зоне входа ничего нет, тезисы не под вопросом.")
    L += ["", "По темам (прогноз прибыли — на следующий год, изменение за 90 дней):"]
    for t in snap["темы"]:
        L += ["", f"{t['название'][:1].upper() + t['название'][1:]}."]
        L += [_bet_line(b) for b in t["бумаги"]]
    nxt = sorted({(b["пересмотр_тезиса"], b["имя"]) for b in allb if b.get("отчёт")})[:3]
    if nxt:
        L += ["", "Ближайшие проверки тезиса (после отчётов): " + "; ".join(f"{n} — {_ru(d)}" for d, n in nxt) + "."]
    tail = ["", "Что делать. Если что-то в зоне входа и тезис тебе по-прежнему близок — напиши в чат «разбери» и "
                "название бумаги, агент проверит свежие новости и посчитает лист ставки. Иначе — ничего.",
            "Это исследовательский инструмент, не инвестиционная рекомендация."]
    body = "\n".join(L)
    room = MAX_LEN - len("\n".join(tail)) - 2
    if len(body) > room:                                           # концовка (что делать, рамка) не обрезается
        body = body[:room].rsplit("\n", 1)[0] + "\n(не поместилось — полный список в reports/long_shelf/latest.json)"
    return body + "\n" + "\n".join(tail)


def _lint(text):
    try:
        import presentation_lint as PL
        return PL.lint_for_user(text, require_sections=PL.DAILY_SECTIONS, max_len=MAX_LEN)
    except Exception as e:                                         # noqa: BLE001
        return [f"линт недоступен: {e}"]


def tick(today=None, push=None, out_dir=None, notices_path=None, fetch=None, themes=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    snap = collect(today, themes=themes, fetch=fetch)
    d = pathlib.Path(out_dir or OUT)
    prev = _prev(today.isoformat(), d)
    d.mkdir(parents=True, exist_ok=True)
    body = json.dumps(snap, ensure_ascii=False, indent=1)
    (d / f"{today.isoformat()}.json").write_text(body, encoding="utf-8")
    (d / "latest.json").write_text(body, encoding="utf-8")
    text = render(snap, prev)
    if push is None:
        try:
            import bot_reports as BR
            push = bool(BR.load_presentation().get("long_shelf_push"))
        except Exception:                                          # noqa: BLE001
            push = False
    viol = _lint(text)
    sent = False
    if push and not viol:
        p = pathlib.Path(notices_path or NOTICES)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                                "text": text, "source": "long_shelf"}, ensure_ascii=False) + "\n")
        sent = True
    return {"снимок": snap, "текст": text, "линт": viol, "отправлено": sent}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Полка длинных тем: недельная проверка")
    ap.add_argument("cmd", choices=["show", "tick"])
    a = ap.parse_args(argv)
    if a.cmd == "show":
        snap = collect()
        print(render(snap, _prev(snap["дата"])))
        print("\n[линт]", _lint(render(snap)) or "ок")
        return 0
    r = tick()
    print(f"[long_shelf] снимок записан; отправлено владельцу: {r['отправлено']}; линт: {r['линт'] or 'ок'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
