# -*- coding: utf-8 -*-
"""ops/scenario_watch.py — СТОРОЖ СЦЕНАРИЕВ (Q-сторож-сценариев, владелец 15.11; ревью 14.11: Fable п.1–2, Codex «Г»).

Зачем: система реагировала только на вопрос владельца. Здесь заранее записанные заготовки «если случится X —
бумага Y» (knowledge/scenarios.yaml) сверяются с миром несколько раз в день; сработало — владельцу в тот же день
одно сообщение: что случилось, что было в прошлые разы (по каждой бумаге — ход в каждом прошлом эпизоде), какая
доля хода уже пройдена, лист ставки в долларах и что опровергнет.

Честность: LLM нет. Прошлые ходы — из knowledge/cascade_history.yaml (посчитаны кодом по закрытиям), арифметика —
mathlib/scenario_stats и mathlib/bet_sheet (Инв#6). Срабатывание ≠ «идея верна» — это «пора смотреть». Журнал
срабатываний journal/scenario_fires.jsonl (append-only, датирован ДО исхода — потом его сверит форвард); журнал
прогнозов не трогается. Черновик заготовки (утверждён: false) считается и пишется в журнал, но владельцу не уходит.

  python3 ops/scenario_watch.py validate          → проверка заготовок (эпизоды, роли с ценами, бумаги в снимке)
  python3 ops/scenario_watch.py check             → что сработало бы сейчас (ничего не пишет, тексты на экран)
  python3 ops/scenario_watch.py show <id>         → текст сообщения заготовки так, будто она сработала сейчас
  python3 ops/scenario_watch.py tick              → боевой проход (бот, флаг scenario_watch_auto): журнал + сообщение
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

from mathlib import scenario_stats as SS      # noqa: E402
from mathlib import bet_sheet as BS           # noqa: E402

SCENARIOS = ROOT / "knowledge" / "scenarios.yaml"
HISTORY = ROOT / "knowledge" / "cascade_history.yaml"
SNAPSHOT = ROOT / "reports" / "prices" / "closes_latest.json"
FIRES = ROOT / "journal" / "scenario_fires.jsonl"
NOTICES = ROOT / "journal" / "notices.jsonl"
NEWSAPI = "https://eventregistry.org/api/v1/article/getArticles"
STAKE_USD = 500.0              # фикс-ставка до доказанной калибровки (как в «Качаем»)
FEES_USD = 2.0                 # прикидка комиссии туда-обратно — помечается в тексте
NEWS_DAYS = 10                 # окно: сегодня + вчера против медианы 7 дней до них
MAX_LEN = 4000
MIN_CASES_SHEET = 2           # лист ставки — только при ≥2 прошлых случаях
SMALL_MOVE_PCT = 3.0         # обычный ход меньше 3% — ставка почти ничего не даёт (пометка в тексте)
SIGMA_MIN = 2.5             # ход датчика должен быть необычным для самой бумаги (в обычных разбросах за срок)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def load_scenarios(path=None):
    return yaml.safe_load(pathlib.Path(path or SCENARIOS).read_text(encoding="utf-8")) or {}


def load_episodes(path=None):
    c = yaml.safe_load(pathlib.Path(path or HISTORY).read_text(encoding="utf-8")) or {}
    for v in c.values():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "trigger_date" in v[0]:
            return {e["id"]: e for e in v}
    return {}


def load_snapshot(path=None):
    try:
        return (json.loads(pathlib.Path(path or SNAPSHOT).read_text(encoding="utf-8")) or {}).get("symbols") or {}
    except (OSError, ValueError):
        return {}


def _read_jsonl(p):
    try:
        return [json.loads(x) for x in pathlib.Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def _append(p, rec):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── прошлые случаи ───────────────────────────────────────────────────────────────────────────────
def bet_history(sc, bet, episodes, horizon):
    eps = [episodes[i] for i in sc.get("эпизоды") or [] if i in episodes]
    moves = SS.episode_moves(eps, role_tag=bet.get("роли"), symbol=bet.get("бумага"), horizon=horizon)
    for m in moves:
        e = episodes.get(m["эпизод"]) or {}
        m["что"] = e.get("title_ru")
        src = (e.get("sources") or [None])[0]
        m["источник"] = src.get("url") if isinstance(src, dict) else src
    why = None                                       # (дата, текст) — из эпизода с самым сильным ходом в нужную сторону
    sign = 1 if bet.get("направление", "up") == "up" else -1
    for m in sorted(moves, key=lambda m: -sign * m["ход_проц"]):
        e = episodes.get(m["эпизод"]) or {}
        for link in e.get("links") or []:
            same = SS._base_sym(link.get("today_symbol")) == SS._base_sym(bet.get("бумага"))
            if (same or link.get("role_tag") in set(bet.get("роли") or [])) and link.get("why_ru"):
                why = (m["дата"], link["why_ru"])
                break
        if why:
            break
    return moves, SS.summary(moves, bet.get("направление", "up")), why


def validate(data=None, episodes=None, snapshot=None):
    """→ [(id заготовки, проблема)] — пусто, если всё хорошо."""
    data = data or load_scenarios()
    episodes = episodes if episodes is not None else load_episodes()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    h = int(data.get("горизонт_дней") or 20)
    out, seen = [], set()
    for sc in data.get("заготовки") or []:
        sid = sc.get("id")
        if not sid or sid in seen:
            out.append((sid, "нет id или id повторяется"))
        seen.add(sid)
        for i in sc.get("эпизоды") or []:
            if i not in episodes:
                out.append((sid, f"эпизода {i} нет в cascade_history"))
        tr = sc.get("триггер") or {}
        if not (tr.get("новости") or tr.get("цена")):
            out.append((sid, "нет триггера"))
        if tr.get("правило") not in ("любой", "оба"):
            out.append((sid, "правило триггера — «любой» или «оба»"))
        if (tr.get("цена") or {}).get("бумага") and tr["цена"]["бумага"] not in snapshot:
            out.append((sid, f"бумаги-датчика {tr['цена']['бумага']} нет в снимке цен"))
        if not sc.get("что_опровергнет"):
            out.append((sid, "нет «что опровергнет»"))
        for b in sc.get("ставки") or []:
            if b.get("бумага") not in snapshot:
                out.append((sid, f"бумаги {b.get('бумага')} нет в снимке цен"))
            if not b.get("имя") or not b.get("роль"):
                out.append((sid, f"у {b.get('бумага')} нет имени или роли"))
            if bet_history(sc, b, episodes, h)[1]["n"] == 0:
                out.append((sid, f"у {b.get('бумага')} нет ни одного прошлого случая с ценами"))
    return out


# ── датчики ──────────────────────────────────────────────────────────────────────────────────────
def news_counts(words, today, key=None, post=None):
    """Статьи по дням (NewsAPI.ai timeAggr, все слова сразу). → {дата: число} или None (нет ключа/сбой)."""
    key = key or os.environ.get("NEWSAPI_AI_KEY")
    if not key and post is None:
        return None
    payload = {"apiKey": key, "resultType": "timeAggr", "keyword": list(words), "keywordOper": "and",
               "dateStart": (today - datetime.timedelta(days=NEWS_DAYS)).isoformat(), "dateEnd": today.isoformat(),
               "lang": "eng", "dataType": ["news"]}
    try:
        if post is not None:
            d = post(payload)
        else:
            req = urllib.request.Request(NEWSAPI, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:                                              # noqa: BLE001 — датчик не роняет сторожа
        return None
    res = ((d or {}).get("timeAggr") or {}).get("results")
    if not isinstance(res, list):
        return None
    return {str(x.get("date"))[:10]: int(x.get("count") or 0) for x in res if isinstance(x, dict)}


def news_signal(counts, today, rule):
    """Всплеск = max(сегодня, вчера) против медианы 7 дней до них. → {сработал, сейчас, норма, кратно, день}."""
    import statistics
    if not counts:
        return {"сработал": False, "причина": "нет данных по новостям"}
    days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(NEWS_DAYS + 1)]
    recent = [(d, counts.get(d, 0)) for d in days[:2]]
    base = [counts[d] for d in days[2:9] if d in counts]
    if len(base) < 5:
        return {"сработал": False, "причина": "нет данных об обычном уровне (меньше 5 дней до всплеска)"}
    day, now = max(recent, key=lambda x: x[1])
    norm = statistics.median(base)
    ratio = (now / norm) if norm else (float("inf") if now else 0.0)
    ok = now >= float(rule.get("мин_статей") or 0) and ratio >= float(rule.get("всплеск_кратно") or 2.0)
    return {"сработал": bool(ok), "сейчас": now, "норма": norm, "кратно": round(ratio, 2) if norm else None,
            "день": day}


def price_signal(rows, rule, now_price=None):
    """Ход бумаги-датчика за N торговых дней (закрытия снимка; текущая цена — если дана). → {сработал, ход_проц,…}."""
    n = int(rule.get("дней") or 3)
    rows = list(rows or [])
    if len(rows) < n + 1:
        return {"сработал": False, "причина": "мало цен в снимке"}
    last_date, last = rows[-1]
    if now_price is not None:
        base_date, base = rows[-n]
        cur, cur_date = float(now_price), "сейчас"
    else:
        base_date, base = rows[-1 - n]
        cur, cur_date = float(last), last_date
    move = (cur / float(base) - 1) * 100
    thr = float(rule.get("ход_проц") or 0)
    ok = move >= thr if thr >= 0 else move <= thr
    # необычность хода для самой бумаги: |ход| ≥ сигм × обычный разброс за тот же срок (дневные закрытия, 60 дней)
    import math
    import statistics
    hist = [float(c) for _, c in rows[-61 - n:-n]] if len(rows) > 61 + n else [float(c) for _, c in rows[:-n]]
    rets = [math.log(b2 / b1) for b1, b2 in zip(hist, hist[1:]) if b1 > 0 and b2 > 0]
    sd = statistics.pstdev(rets) if len(rets) >= 20 else None
    z = (math.log(cur / float(base)) / (sd * math.sqrt(n))) if sd else None
    need = float(rule.get("сигм") or SIGMA_MIN)
    if z is None or abs(z) < need:
        ok = False
    return {"сработал": bool(ok), "ход_проц": round(move, 2), "от": [base_date, base], "до": [cur_date, round(cur, 4)],
            "порог_проц": thr, "сигм": round(z, 2) if z is not None else None, "порог_сигм": need}


def realtime_prices(symbols, key=None, get=None):
    """Текущие цены EODHD real-time пачкой (во время сессии). → {символ: цена}; сбой — {}."""
    key = key or os.environ.get("EODHD_API_KEY")
    syms = sorted(set(symbols))
    if not syms or (not key and get is None):
        return {}
    url = (f"https://eodhd.com/api/real-time/{syms[0]}?fmt=json" + (f"&s={','.join(syms[1:])}" if syms[1:] else "")
           + (f"&api_token={key}" if key else ""))
    try:
        raw = get(url) if get is not None else urllib.request.urlopen(url, timeout=60).read()
        data = json.loads(raw)
    except Exception:                                              # noqa: BLE001
        return {}
    out = {}
    for r in data if isinstance(data, list) else [data]:
        if isinstance(r, dict) and r.get("code") and isinstance(r.get("close"), (int, float)) and r["close"] > 0:
            code = str(r["code"])
            out[code if "." in code else code + ".US"] = float(r["close"])
    return out


def evaluate(sc, today, snapshot, news_fn=None, live=None):
    """Сработала ли заготовка. → {сработала, новости, цена, почему}."""
    tr = sc.get("триггер") or {}
    res = {"новости": None, "цена": None}
    if tr.get("новости"):
        counts = (news_fn or news_counts)(tr["новости"]["слова"], today)
        res["новости"] = news_signal(counts, today, tr["новости"])
    if tr.get("цена"):
        sym = tr["цена"]["бумага"]
        res["цена"] = price_signal(snapshot.get(sym), tr["цена"], (live or {}).get(sym))
    hits = [v["сработал"] for v in res.values() if v is not None]
    fired = (all(hits) if tr.get("правило") == "оба" else any(hits)) if hits else False
    res["сработала"] = bool(fired)
    return res


# ── текст владельцу ──────────────────────────────────────────────────────────────────────────────
def _usd(x):
    return f"${abs(float(x)):,.0f}".replace(",", " ")


def _money(x):
    return f"${float(x):,.2f}".replace(",", " ")


_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"]


def _ru_date(iso):
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
        return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"
    except ValueError:
        return str(iso)


def trigger_date(sc, ev, snapshot):
    """Дата повода: день новостного всплеска или начало ценового окна датчика — что раньше. None — повода нет."""
    c = []
    nw, pr = ev.get("новости") or {}, ev.get("цена") or {}
    if nw.get("сработал") and nw.get("день"):
        c.append(nw["день"])
    if pr.get("сработал") and pr.get("от"):
        rows = snapshot.get(((sc.get("триггер") or {}).get("цена") or {}).get("бумага")) or []
        nxt = [r[0] for r in rows if r[0] > pr["от"][0]]
        c.append(nxt[0] if nxt else pr["от"][0])     # первый день хода; база — закрытие перед ним
    return min(c) if c else None


def price_before(rows, day):
    """Последнее закрытие строго до дня повода (без повода — последнее закрытие). → (дата, цена) или (None, None)."""
    rows = list(rows or [])
    before = [r for r in rows if day is None or r[0] < day]
    return tuple(before[-1]) if before else (None, None)


def _then_note(then, sym):
    """Пояснение, по какой бумаге считали прошлый ход, если не по самой (простыми словами, без биржевых кодов)."""
    t = str(then or "")
    if not t or SS._base_sym(t) == SS._base_sym(sym):
        return ""
    if t.endswith("=F"):
        return " (тогда — по биржевой цене самого сырья)"
    if t.startswith("FRED:") or t.startswith("^"):
        return " (тогда — по индексу или ряду цен)"
    return f" (тогда — по похожей компании {t})"


def bet_block(sc, bet, episodes, snapshot, live, ev, horizon, day=None):
    sym = bet["бумага"]
    rows = snapshot.get(sym) or []
    moves, summ, why = bet_history(sc, bet, episodes, horizon)
    up = bet.get("направление", "up") == "up"
    label = f"{bet['имя']} ({sym})"
    L = [f"— {label}: {bet['роль']}."]
    if why:
        L.append(f"Из разбора случая {_ru_date(why[0])}: «{why[1].rstrip('.')}».")
    if moves:
        past = "; ".join(f"{_ru_date(m['дата'])} — {'+' if m['ход_проц'] >= 0 else '−'}{abs(m['ход_проц']):.0f}%"
                         + _then_note(m.get("бумага_тогда"), sym)
                         for m in moves)
        L.append(f"В прошлые разы за {horizon} торговых дней после повода: {past}.")
        if summ.get("мало_случаев"):
            L.append(f"Случаев всего {summ['n']} — это ориентир, а не закономерность.")
        else:
            L.append(f"Из {summ['n']} случаев бумага пошла в нужную сторону в {summ['в_сторону']}; "
                     f"середина ряда — {summ['медиана_проц']:+.0f}%.")
    entry = (live or {}).get(sym) or (rows[-1][1] if rows else None)
    base = price_before(rows, day)
    typical = summ.get("медиана_проц")
    if entry is None or base[1] is None or not typical or (up and typical <= 0) or (not up and typical >= 0):
        L.append("Лист ставки не считаю: " + ("нет цены" if entry is None else
                                              "в прошлые разы бумага в среднем не шла в нужную сторону") + ".")
        return "\n".join(L), {"бумага": sym, "ставка": None, "ход": summ}
    if summ["n"] < MIN_CASES_SHEET:
        L.append(f"Цена до повода {_money(base[1])} ({_ru_date(base[0])}), сейчас {_money(entry)}. "
                 "Лист ставки не считаю: по одному прошлому случаю цель и порог были бы ложной точностью.")
        return "\n".join(L), {"бумага": sym, "ставка": None, "ход": summ}
    frac = SS.passed(base[1], entry, typical)
    lv = SS.levels(entry, base[1], typical, "up" if up else "down")
    L.append(f"Цена до повода {_money(base[1])} ({_ru_date(base[0])}), сейчас {_money(entry)}"
             + ("." if frac is None else " — ход ещё не начался." if frac < 0.05 else
                f" — уже пройдено около {frac * 100:.0f}% обычного хода."))
    if "стоп" not in lv:
        L.append("Обычный ход уже пройден — догонять поздно, ставки нет.")
        return "\n".join(L), {"бумага": sym, "ставка": None, "ход": summ, "пройдено": frac}
    sheet = BS.sheet(entry, [{"name": "цель", "price": lv["цель"]}, {"name": "без движения", "price": entry},
                             {"name": "стоп", "price": lv["стоп"]}], size_usd=STAKE_USD, fees_usd=FEES_USD,
                     direction="long" if up else "short")
    if not sheet.get("ok"):
        L.append(f"Лист ставки не получился: {sheet.get('причина')}.")
        return "\n".join(L), {"бумага": sym, "ставка": None, "ход": summ}
    r = {x["name"]: x for x in sheet["сценарии"]}
    small = abs(typical) < SMALL_MOVE_PCT
    n = sheet.get("порог_шанса")
    n100 = min(99, max(1, int(-(-n * 100 // 1)))) if n is not None else None
    L.append(f"Ставка {_usd(STAKE_USD)}: если бумага повторит обычный ход (до {_money(lv['цель'])}) — "
             f"заработок около {_usd(r['цель']['pnl_usd'])}; если пойдёт против и сработает выход на "
             f"{_money(lv['стоп'])} (половина обычного хода против нас) — потеря около {_usd(r['стоп']['pnl_usd'])}."
             + (f" Ставка в плюсе, если хороший исход случается хотя бы в {n100} из 100 случаев." if n100 else "")
             + (f" Обычный ход мал ({typical:+.0f}%) — такая ставка почти ничего не даёт." if small else ""))
    return "\n".join(L), {"бумага": sym, "вход": entry, "до_повода": base, "цель": lv["цель"], "стоп": lv["стоп"],
                          "пройдено": frac, "порог_шанса": n, "ход": summ}


def render(sc, ev, episodes, snapshot, live=None, horizon=20, draft=False):
    L = [f"Сработала заготовка: {sc['название']}.", "",
         "Что это значит для тебя. Мы заранее записали: «если случится такое — смотреть на эти бумаги». "
         "Похоже, это случилось. Это не вывод «покупай», а сигнал «пора смотреть» — с расчётом, сколько можно "
         "заработать и потерять.", ""]
    why = []
    nw, pr = ev.get("новости") or {}, ev.get("цена") or {}
    if nw.get("сработал"):
        why.append(f"статей на эту тему за день — {nw['сейчас']}, обычно около {nw['норма']:.0f}"
                   + (f" (в {nw['кратно']:.1f} раза больше)" if nw.get("кратно") else ""))
    if pr.get("сработал"):
        tr = sc["триггер"]["цена"]
        why.append(f"{tr['бумага']} за {tr.get('дней')} торговых дня {'выросла' if pr['ход_проц'] >= 0 else 'упала'} "
                   f"на {abs(pr['ход_проц']):.1f}%")
    what = sc.get("что_случилось", "").rstrip(".")
    what = what[:1].upper() + what[1:]
    L.append("Что случилось. " + what + ". Признаки: " + "; ".join(why) + "." if why else "Что случилось. " + what + ".")
    L += ["", "На что смотреть:"]
    bets = []
    day = trigger_date(sc, ev, snapshot)
    for b in sc.get("ставки") or []:
        txt, meta = bet_block(sc, b, episodes, snapshot, live, ev, horizon, day)
        L += [txt, ""]
        bets.append(meta)
    if sc.get("что_опровергнет"):
        L.append("Что опровергнет: " + "; ".join(x.rstrip(".") for x in sc["что_опровергнет"]) + ".")
    L += ["", "Что делать. Решить, хочешь ли ты в это играть. Если да — напиши в чат «разбери» и название бумаги, "
              "агент проверит свежие новости и цены. Если нет — ничего, заготовка замолчит на две недели.",
          "Это исследовательский инструмент, не инвестиционная рекомендация."]
    if draft:
        L.insert(0, "[Черновик заготовки — владельцу не отправлялось]")
    text = "\n".join(L)
    return text[:MAX_LEN], bets


def _lint(text):
    try:
        import presentation_lint as PL
        viol = PL.lint_for_user(text, require_sections=PL.DAILY_SECTIONS, max_len=MAX_LEN)
        if PL._TECH_ID_RE.search(text):
            viol.append("тех.id в тексте для владельца")
        import re
        if re.search(r"\b[a-z]+_[a-z0-9_]+\b", text):
            viol.append("внутренний идентификатор в тексте для владельца")
        return viol
    except Exception as e:                                         # noqa: BLE001
        return [f"линт недоступен: {e}"]


# ── проход ───────────────────────────────────────────────────────────────────────────────────────
def run(now=None, write=False, data=None, episodes=None, snapshot=None, news_fn=None, live_fn=None,
        fires_path=None, notices_path=None, only=None, force=False):
    """Один проход сторожа. write=False — ничего не пишет. force=True — считать сработавшей (для показа).
    → {"сработали": [...], "тексты": [...], "ошибки": [...]}."""
    now = now or _now()
    today = now.date()
    data = data or load_scenarios()
    episodes = episodes if episodes is not None else load_episodes()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    horizon = int(data.get("горизонт_дней") or 20)
    pause = int(data.get("пауза_дней") or 14)
    fires = _read_jsonl(fires_path or FIRES)
    out = {"ts": now.isoformat(timespec="seconds"), "сработали": [], "тексты": [], "ошибки": [], "проверено": 0}
    scs = [s for s in data.get("заготовки") or [] if not only or s.get("id") == only]
    syms = {b["бумага"] for s in scs for b in s.get("ставки") or []} | \
           {(s.get("триггер") or {}).get("цена", {}).get("бумага") for s in scs if (s.get("триггер") or {}).get("цена")}
    live = (live_fn or realtime_prices)([x for x in syms if x]) if live_fn is not False else {}
    for sc in scs:
        sid = sc.get("id")
        out["проверено"] += 1
        recent = [f for f in fires if f.get("id") == sid and f.get("ts", "")[:10] >=
                  (today - datetime.timedelta(days=pause)).isoformat()]
        if recent and not force:
            continue
        try:
            ev = evaluate(sc, today, snapshot, news_fn=news_fn, live=live)
            if not ev["сработала"] and not force:
                continue
            draft = not sc.get("утверждён")
            text, bets = render(sc, ev, episodes, snapshot, live, horizon, draft=draft)
            viol = _lint(text.replace("[Черновик заготовки — владельцу не отправлялось]\n", ""))
            if viol:
                out["ошибки"].append({"id": sid, "ошибка": f"линт: {viol[:2]}"})
                continue
            rec = {"ts": now.isoformat(timespec="seconds"), "id": sid, "утверждён": bool(sc.get("утверждён")),
                   "датчики": ev, "ставки": bets, "отправлено": bool(write and not draft)}
            out["сработали"].append(rec)
            out["тексты"].append(text)
            if write:
                _append(fires_path or FIRES, rec)
                if not draft:
                    _append(notices_path or NOTICES, {"ts": rec["ts"], "text": text, "source": "scenario_watch"})
        except Exception as e:                                     # noqa: BLE001 — одна заготовка ≠ весь проход
            out["ошибки"].append({"id": sid, "ошибка": f"{type(e).__name__}: {e}"})
    return out


def replay(days=30, today=None, data=None, episodes=None, snapshot=None, news_fn=None, only=None):
    """Прогон «задним числом» (НЕ форвард): когда сработал бы сторож за последние days дней. Новости — дневные
    итоги, какими они видны СЕЙЧАС (в тот день до вечера статей было видно меньше), цены — закрытия снимка до дня.
    Пауза между срабатываниями соблюдается. → [{"день", "id", "текст"}]."""
    import tempfile
    today = today or _now().date()
    data = data or load_scenarios()
    episodes = episodes if episodes is not None else load_episodes()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    cache = {}

    def cached(words, _day):
        k = tuple(words)
        if k not in cache:
            global NEWS_DAYS
            old, NEWS_DAYS = NEWS_DAYS, days + 10
            try:
                cache[k] = (news_fn or news_counts)(words, today)
            finally:
                NEWS_DAYS = old
        return cache[k]
    out = []
    with tempfile.TemporaryDirectory() as td:
        fp, npth = pathlib.Path(td) / "f.jsonl", pathlib.Path(td) / "n.jsonl"
        for i in range(days, -1, -1):
            d = today - datetime.timedelta(days=i)
            snap_d = {k: [r for r in v if r[0] <= d.isoformat()] for k, v in snapshot.items()}
            now = datetime.datetime.combine(d, datetime.time(20, 0), tzinfo=datetime.timezone.utc)
            r = run(now=now, write=True, data=data, episodes=episodes, snapshot=snap_d, news_fn=cached,
                    live_fn=False, fires_path=fp, notices_path=npth, only=only)
            for rec, text in zip(r["сработали"], r["тексты"]):
                out.append({"день": d.isoformat(), "id": rec["id"], "текст": text, "датчики": rec["датчики"]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сторож сценариев: заготовки «если X — бумага Y»")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("check")
    s = sub.add_parser("show")
    s.add_argument("id")
    sub.add_parser("tick")
    rp = sub.add_parser("replay")
    rp.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "replay":
        rs = replay(a.days)
        for x in rs:
            nw, pr = x["датчики"].get("новости") or {}, x["датчики"].get("цена") or {}
            print(f"{x['день']}  {x['id']:26} новости {nw.get('сейчас')}/{nw.get('норма')} цена {pr.get('ход_проц')}%")
        print(f"[scenario_watch] задним числом за {a.days} дн.: срабатываний {len(rs)} (не форвард — оговорка в replay)")
        return 0
    if a.cmd == "validate":
        probs = validate()
        for sid, p in probs:
            print(f"[{sid}] {p}")
        print(f"[scenario_watch] заготовок проверено, проблем: {len(probs)}")
        return 1 if probs else 0
    if a.cmd == "show":
        r = run(write=False, only=a.id, force=True)
    else:
        r = run(write=(a.cmd == "tick"))
    for t in r["тексты"]:
        print("\n" + "─" * 60 + "\n" + t)
    for e in r["ошибки"]:
        print("[ошибка]", e)
    print(f"[scenario_watch] проверено {r['проверено']}, сработало {len(r['сработали'])}, ошибок {len(r['ошибки'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
