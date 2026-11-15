# -*- coding: utf-8 -*-
"""ops/ledger.py — общий счёт: ВСЕ предложения системы + реальные сделки владельца против эталона 60/40.

ПОЧЕМУ (два внешних ревью 14.11, Codex п.6 и Fable п.6; да владельца «делай до упора»): журнал прогнозов
меряет «закрылась ли цена за порогом», а не «что мы решили и что это дало». Реальная покупка BRNT 10.11
в счёт не попала вовсе. Здесь — один ответ на вопрос «стоило ли слушать систему»:
  • предложение = идея, которую система выдвинула владельцу (запечатанные hunter_idea/chat_idea из журнала
    прогнозов + предложения из разговора, записанные вручную со ссылкой на источник). Условный вход — по цене
    в день предложения (якорь), равная сумма на каждое; это МОДЕЛЬ, не сделка;
  • сделка = то, что владелец реально купил/продал (его слова или подтверждение брокера), отдельно от модели;
  • эталон = 60% SPY + 40% DBC (§30) с той же даты — иначе рост рынка выглядит «умом».
Только дописывание (journal/trades.jsonl, journal/proposals_manual.jsonl); математика — код (Инв#6);
цены — снимок сервера, иначе EODHD; нет цены — честное «нет цены» (П8). Мало записей = вывода об умении нет.
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))

TRADES = ROOT / "journal" / "trades.jsonl"
MANUAL = ROOT / "journal" / "proposals_manual.jsonl"
PRED = ROOT / "journal" / ("predictions" + ".jsonl")
SEALED_KINDS = ("hunter_idea", "chat_idea")
BENCH = (("SPY.US", 0.6), ("DBC.US", 0.4))
NOTIONAL = 1000.0
MIN_N_VERDICT = 30          # меньше — только «рано судить»


def _read(p):
    p = pathlib.Path(p)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _append(p, rec):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sym(t):
    t = str(t or "").upper().strip()
    return t if "." in t else t + ".US"


# ── запись ──
def add_trade(ticker, side, qty, price, *, currency="USD", fee=None, fee_currency=None, date=None,
              source="", owner_words="", idea_ref=None, path=TRADES):
    """Реальная сделка владельца. Без источника (слова владельца / подтверждение брокера) не пишется."""
    side = str(side).lower()
    if side not in ("buy", "sell"):
        raise ValueError("сторона — buy или sell")
    if not str(source).strip() and not str(owner_words).strip():
        raise ValueError("сделка без источника (слова владельца или подтверждение брокера) не записывается — П8")
    q, px = float(qty), float(price)
    if q <= 0 or px <= 0:
        raise ValueError("количество и цена — положительные числа")
    return _append(path, {"ts": _now(), "вид": "сделка", "тикер": _sym(ticker), "сторона": side, "кол_во": q,
                          "цена": px, "валюта": currency, "комиссия": fee, "валюта_комиссии": fee_currency,
                          "дата": str(date)[:10] if date else None, "источник": source,
                          "слова_владельца": owner_words, "идея": idea_ref})


def add_proposal(ticker, direction, date, *, price=None, thesis="", source="", horizon=None, path=MANUAL):
    """Предложение системы из разговора (не запечатанное): со ссылкой на источник (строка журнала разговора)."""
    if direction not in ("above", "below"):
        raise ValueError("направление — above (покупка) или below (против бумаги)")
    if not str(source).strip():
        raise ValueError("предложение без ссылки на источник не записывается — П8")
    return _append(path, {"ts": _now(), "вид": "предложение", "тикер": _sym(ticker), "direction": direction,
                          "дата": str(date)[:10], "цена": float(price) if price else None,
                          "тезис": str(thesis)[:400], "источник": source, "горизонт": horizon})


# ── цены ──
class Prices:
    """Закрытия: снимок сервера (US), иначе EODHD (нужен EODHD_API_KEY). Кэш на прогон."""

    def __init__(self, fetch=None):
        self._cache = {}
        self._fetch = fetch

    def series(self, sym, date_from):
        key = (sym, date_from)
        if key in self._cache:
            return self._cache[key]
        rows = None
        if self._fetch is not None:
            rows = self._fetch(sym, date_from)
        else:
            rows = self._from_snapshot(sym, date_from) or self._from_eodhd(sym, date_from)
        self._cache[key] = rows or []
        return self._cache[key]

    @staticmethod
    def _from_snapshot(sym, date_from):
        try:
            from ops import price_snapshot as PS
        except ImportError:
            import price_snapshot as PS
        try:
            snap = json.loads(pathlib.Path(PS.OUT).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        rows = (snap.get("symbols") or {}).get(sym)
        if not rows or rows[0][0] > date_from:
            return None
        return [(d, float(c)) for d, c in rows if d >= date_from]

    @staticmethod
    def _from_eodhd(sym, date_from):
        key = os.environ.get("EODHD_API_KEY")
        if not key:
            return None
        try:
            from data import eodhd as E
            rows = E.fetch_eod(sym, key, date_from, datetime.date.today().isoformat())
        except Exception:                                   # noqa: BLE001 — нет цены = честное «нет цены»
            return None
        return [(r["date"], float(r.get("adjusted_close") or r["close"])) for r in rows if r.get("close")]


def _ret(rows, start_px=None, stop=None, direction="above"):
    """→ (цена входа, дата входа, цена выхода/последняя, дата, доходность, вышли_по_стопу) или None.
    Стоп — правило выхода из карточки: первое закрытие за стопом закрывает условную позицию по нему."""
    if not rows:
        return None
    d0, p0 = rows[0]
    p0 = float(start_px) if start_px else p0
    if isinstance(stop, (int, float)) and stop > 0:
        for d, c in rows[1:]:
            if (direction == "above" and c <= stop) or (direction == "below" and c >= stop):
                return p0, d0, c, d, c / p0 - 1, True
    d1, p1 = rows[-1]
    return p0, d0, p1, d1, p1 / p0 - 1, False


def bench_return(prices, date_from, date_to=None):
    """Эталон 60/40 с даты входа по дату выхода (иначе — по последнее закрытие)."""
    tot, parts = 0.0, {}
    for sym, w in BENCH:
        ser = prices.series(sym, date_from)
        if date_to:
            ser = [x for x in ser if x[0] <= date_to] or ser[:1]
        r = _ret(ser)
        if r is None:
            return None, {}
        parts[sym] = r[4]
        tot += w * r[4]
    return tot, parts


# ── сборка ──
SCENARIO_FIRES = ROOT / "journal" / "scenario_fires.jsonl"
SHELF_DIR = ROOT / "reports" / "long_shelf"


def _scenario_proposals(path=None):
    """Сторож сценариев: только ушедшие владельцу срабатывания и только бумаги с листом ставки (вход посчитан)."""
    out = []
    for r in _read(path or SCENARIO_FIRES):
        if not r.get("отправлено"):
            continue
        for b in r.get("ставки") or []:
            if b.get("вход") is None:
                continue
            out.append({"тикер": b["бумага"], "direction": b.get("направление") or "above",
                        "дата": str(r.get("ts"))[:10], "цена": b["вход"], "цель": b.get("цель"), "стоп": b.get("стоп"),
                        "источник": f"сторож сценариев «{r.get('id')}» {str(r.get('ts'))[:16]}", "трек": "сторож"})
    return out


def _shelf_proposals(shelf_dir=None):
    """Полка длинных тем: бумага впервые «в зоне входа» в ОТПРАВЛЕННОЙ владельцу сводке — по цене снимка."""
    d = pathlib.Path(shelf_dir or SHELF_DIR)
    if not d.is_dir():
        return []
    out, was = [], {}
    for p in sorted(d.glob("20*.json")):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not snap.get("отправлено"):
            continue
        for t in snap.get("темы") or []:
            for b in t.get("бумаги") or []:
                sym, st = b.get("бумага"), b.get("статус")
                if st == "зона входа" and was.get(sym) != "зона входа" and (b.get("годовой") or {}).get("сейчас"):
                    out.append({"тикер": sym, "direction": "above", "дата": snap["дата"],
                                "цена": b["годовой"]["сейчас"], "источник": f"полка длинных тем {snap['дата']}",
                                "трек": "полка", "тезис": t.get("название")})
                was[sym] = st
    return out


def proposals(pred_path=PRED, manual_path=MANUAL, fires_path=None, shelf_dir=None):
    from mathlib import sealing as SEAL
    recs = SEAL.drop_voided(_read(pred_path), pred_path)
    out = []
    for r in recs:
        if r.get("kind") in SEALED_KINDS and r.get("asset") and r.get("якорь_дата"):
            out.append({"тикер": r["asset"], "direction": r.get("direction"), "дата": r["якорь_дата"],
                        "цена": r.get("якорь_цена"), "источник": f"журнал прогнозов {r['kind']} {r['hash'][:12]}",
                        "трек": r["kind"], "цель": r.get("цель") or r.get("threshold"), "стоп": r.get("стоп"),
                        "вход_не_хуже": r.get("вход_не_хуже"), "срок": str(r.get("resolve_by") or "")[:10]})
    for m in _read(manual_path):
        out.append({"тикер": m["тикер"], "direction": m["direction"], "дата": m["дата"], "цена": m.get("цена"),
                    "источник": m["источник"], "трек": "разговор", "тезис": m.get("тезис"),
                    "срок": m.get("горизонт")})
    out += _scenario_proposals(fires_path) + _shelf_proposals(shelf_dir)
    return sorted(out, key=lambda x: x["дата"])


def positions(trades):
    pos = {}
    for t in trades:
        p = pos.setdefault(t["тикер"], {"кол_во": 0.0, "вложено": 0.0, "комиссии": 0.0, "первая_дата": t.get("дата")})
        sign = 1 if t["сторона"] == "buy" else -1
        p["кол_во"] += sign * t["кол_во"]
        p["вложено"] += sign * t["кол_во"] * t["цена"]
        if isinstance(t.get("комиссия"), (int, float)):
            p["комиссии"] += t["комиссия"]
        p["валюта_комиссии"] = t.get("валюта_комиссии")
        p["валюта"] = t.get("валюта")
    return pos


def build(prices=None, pred_path=PRED, manual_path=MANUAL, trades_path=TRADES, fires_path=None, shelf_dir=None):
    prices = prices or Prices()
    rows = []
    for pr in proposals(pred_path, manual_path, fires_path, shelf_dir):
        r = _ret(prices.series(pr["тикер"], pr["дата"]), pr.get("цена"), pr.get("стоп"), pr["direction"])
        b, _ = bench_return(prices, pr["дата"], r[3] if r else None)
        row = dict(pr)
        if r is None:
            row["нет_цены"] = True
        else:
            sign = 1 if pr["direction"] == "above" else -1
            row.update({"вход": round(r[0], 4), "последняя": round(r[2], 4), "на_дату": r[3], "по_стопу": r[5],
                        "доходность": round(sign * r[4], 4),
                        "эталон": round(b, 4) if b is not None else None,
                        "сверх_эталона": round(sign * r[4] - b, 4) if b is not None else None})
        rows.append(row)
    trades = _read(trades_path)
    pos_rows = []
    for sym, p in positions(trades).items():
        ser = prices.series(sym, p["первая_дата"] or "2026-01-01")
        last = ser[-1] if ser else None
        row = {"тикер": sym, **{k: round(v, 4) if isinstance(v, float) else v for k, v in p.items()}}
        if last and p["кол_во"]:
            val = p["кол_во"] * last[1]
            row.update({"последняя": last[1], "на_дату": last[0], "стоимость": round(val, 2),
                        "результат_до_комиссий": round(val - p["вложено"], 2)})
            if p["первая_дата"]:
                b, _ = bench_return(prices, p["первая_дата"])
                row["эталон_с_даты"] = round(b, 4) if b is not None else None
        else:
            row["нет_цены"] = True
        pos_rows.append(row)
    done = [r for r in rows if "доходность" in r]
    summ = {"предложений": len(rows), "с_ценой": len(done),
            "средняя_доходность": round(sum(r["доходность"] for r in done) / len(done), 4) if done else None,
            "средняя_эталона": (round(sum(r["эталон"] for r in done if r["эталон"] is not None)
                                 / max(1, sum(r["эталон"] is not None for r in done)), 4) if done else None),
            "средняя_сверх_эталона": (round(sum(r["сверх_эталона"] for r in done if r["сверх_эталона"] is not None)
                                      / max(1, sum(r["сверх_эталона"] is not None for r in done)), 4)
                                      if done else None),
            "вывод": ("рано судить: меньше %d предложений, и ни одно не прошло срок" % MIN_N_VERDICT
                      if len(done) < MIN_N_VERDICT else "выборка достаточна для первого вывода")}
    return {"построено": _now(), "предложения": rows, "сделки": trades, "позиции": pos_rows, "итог": summ}


TRACK_WORDS = {"hunter_idea": "идея охотника", "chat_idea": "идея из разговора", "разговор": "из разговора",
               "сторож": "сторож сценариев", "полка": "полка длинных тем"}


def _pct(x):
    return "—" if x is None else f"{x * 100:+.1f}%"


def render(rep):
    """Человеческим языком для Telegram/агента."""
    s = rep["итог"]
    L = ["Общий счёт: всё, что предлагала система, и ваши сделки — против эталона 60% SPY + 40% DBC."]
    for r in rep["предложения"]:
        side = "покупка" if r["direction"] == "above" else "против бумаги"
        if r.get("нет_цены"):
            L.append(f"• {r['тикер']} ({side}, с {r['дата']}): нет цены — пересчитать нечем")
        else:
            now = "вышли по стопу" if r.get("по_стопу") else "сейчас"
            L.append(f"• {r['тикер']} ({side}, {TRACK_WORDS.get(r.get('трек'), r.get('трек'))}, с {r['дата']} по {r['вход']:g}): {now} {r['последняя']:g} "
                     f"на {r['на_дату']} → {_pct(r['доходность'])}, эталон {_pct(r['эталон'])}")
    for p in rep["позиции"]:
        if p.get("нет_цены"):
            L.append(f"Ваша позиция {p['тикер']}: {p['кол_во']:g} шт., цены нет")
        else:
            cur = f" {p['валюта_комиссии']}" if p.get("валюта_комиссии") else ""
            fee = (f"; комиссия брокера {p['комиссии']:g}{cur}, за вычетом её "
                   f"{p['результат_до_комиссий'] - p['комиссии']:+.2f}" if p.get("комиссии") and not cur else
                   f"; комиссия брокера {p['комиссии']:g}{cur}" if p.get("комиссии") else "")
            m = f" {p['валюта']}" if p.get("валюта") else ""
            L.append(f"Ваша позиция {p['тикер']}: {p['кол_во']:g} шт., вложено {p['вложено']:.2f}{m}, сейчас стоит "
                     f"{p['стоимость']:.2f}{m} — изменение {p['результат_до_комиссий']:+.2f}{fee}; "
                     f"эталон с той же даты {_pct(p.get('эталон_с_даты'))}")
    gap = s.get("средняя_сверх_эталона")
    cmp_ = ("" if gap is None or s.get("средняя_эталона") is None else
            f"; эталон за те же даты в среднем {_pct(s['средняя_эталона'])} — мы "
            f"{'хуже' if gap < 0 else 'лучше'} его на {abs(gap) * 100:.1f} п.п" if abs(gap) >= 0.0005 else
            f"; эталон за те же даты в среднем {_pct(s['средняя_эталона'])} — вровень с ним")
    L.append(f"Итого предложений {s['предложений']}: если бы на каждое вложили одинаковую сумму, в среднем "
             f"{_pct(s['средняя_доходность'])}{cmp_}. Это условный вход по цене дня предложения, "
             f"а не сделки. Вывод: {s['вывод']}.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show").add_argument("--json", action="store_true")
    t = sub.add_parser("add-trade")
    for a in ("ticker", "side", "qty", "price"):
        t.add_argument(a)
    t.add_argument("--currency", default="USD")
    t.add_argument("--fee", type=float)
    t.add_argument("--fee-currency")
    t.add_argument("--date")
    t.add_argument("--source", default="")
    t.add_argument("--owner-words", default="")
    t.add_argument("--idea")
    p = sub.add_parser("add-proposal")
    for a in ("ticker", "direction", "date"):
        p.add_argument(a)
    p.add_argument("--price", type=float)
    p.add_argument("--thesis", default="")
    p.add_argument("--source", default="")
    p.add_argument("--horizon")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        rep = build()
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str) if a.json else render(rep))
    elif a.cmd == "add-trade":
        print(json.dumps(add_trade(a.ticker, a.side, a.qty, a.price, currency=a.currency, fee=a.fee,
                                   fee_currency=a.fee_currency, date=a.date, source=a.source,
                                   owner_words=a.owner_words, idea_ref=a.idea), ensure_ascii=False))
    else:
        print(json.dumps(add_proposal(a.ticker, a.direction, a.date, price=a.price, thesis=a.thesis,
                                      source=a.source, horizon=a.horizon), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
