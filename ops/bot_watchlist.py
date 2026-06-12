# -*- coding: utf-8 -*-
"""ops/bot_watchlist.py — лист ожидания триггеров (§17 «обновление листа ожидания триггеров»,
агент своевременности §4 блок D: вердикт РАНО → «войти, когда произойдёт Y» — мониторим триггер).

Журнал journal/watchlist.jsonl — append-only поток СОБЫТИЙ (§16, ничего не удаляется):
  • add    — постановка идеи в лист ожидания;
  • fire   — структурный триггер сработал (актив пересёк уровень по данным oracle.db);
  • cancel — снятие записи (вручную / идея протухла).
Текущее состояние = свёртка событий (current_entries): запись «armed», пока нет fire/cancel.

Наполнение (решение пользователя): (1) бот ИЗВЛЕКАЕТ РАНО-идеи из journal/funnel_logs/*.json и
ставит их в лист как «armed, ручная проверка» (структурного условия в протоколе нет — П8: не
выдумываем уровень); (2) оператор привязывает СТРУКТУРНЫЙ ценовой триггер командой CLI.

Авто-проверка (решение пользователя): бот сам сверяет ТОЛЬКО структурный ценовой триггер
(актив пересёк уровень X сверху/снизу по close из oracle.db). Свободнотекстовые «войти, когда Y»
помечаются manual_check=True и НЕ порождают авто-алертов (П8 — честно о пробеле).
"""
import json
import sys
import sqlite3
import hashlib
import argparse
import datetime
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
WATCHLIST_PATH = ROOT / "journal" / "watchlist.jsonl"
DB_PATH = ROOT / "storage" / "oracle.db"

TRIGGER_DIRS = ("above", "below")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _entry_id(source, asset, trigger_text):
    """Контентный id — стабильный дедуп: повторное извлечение той же идеи не плодит записей."""
    raw = f"{source}|{asset}|{trigger_text or ''}"
    return "wl_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ── чтение/запись событий ──────────────────────────────────────────────────────────
def read_events(path=None):
    p = pathlib.Path(path or WATCHLIST_PATH)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append(record, path=None):
    p = pathlib.Path(path or WATCHLIST_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def current_entries(path=None):
    """Свёртка событий → {id: entry} только для записей в статусе 'armed' (нет fire/cancel)."""
    events = read_events(path or WATCHLIST_PATH)
    entries = {}
    closed = set()
    for ev in events:
        t = ev.get("type")
        eid = ev.get("id")
        if t == "add":
            entries[eid] = {**ev, "status": "armed"}
        elif t in ("fire", "cancel"):
            closed.add(eid)
    for eid in closed:
        entries.pop(eid, None)
    return entries


# ── постановка в лист ожидания ──────────────────────────────────────────────────────
def make_trigger(symbol, level, direction):
    """Структурный ценовой триггер: актив пересёк уровень сверху/снизу."""
    if direction not in TRIGGER_DIRS:
        raise ValueError(f"direction триггера ∈ {TRIGGER_DIRS}, получено {direction!r}")
    return {"type": "price_cross", "symbol": str(symbol),
            "level": float(level), "dir": direction}


def add_entry(*, asset, direction=None, trigger_text=None, trigger=None,
              source="cli", path=None):
    """Поставить идею в лист ожидания. trigger=None → запись manual_check (только напоминания)."""
    eid = _entry_id(source, asset, trigger_text or (json.dumps(trigger, sort_keys=True) if trigger else ""))
    rec = {
        "ts": _now_iso(),
        "type": "add",
        "id": eid,
        "asset": asset,
        "direction": direction,          # long/short тезиса (справочно)
        "trigger_text": trigger_text,    # свободный текст «войти, когда Y»
        "trigger": trigger,              # структурный price_cross или None
        "manual_check": trigger is None, # нет машинного условия → ручная проверка (П8)
        "source": source,
    }
    return _append(rec, path or WATCHLIST_PATH)


def fire_entry(entry, observed, path=None):
    rec = {
        "ts": _now_iso(),
        "type": "fire",
        "id": entry["id"],
        "asset": entry.get("asset"),
        "trigger": entry.get("trigger"),
        "observed": observed,            # {date, close}
    }
    return _append(rec, path or WATCHLIST_PATH)


def cancel_entry(eid, reason="manual", path=None):
    return _append({"ts": _now_iso(), "type": "cancel", "id": eid, "reason": reason},
                   path or WATCHLIST_PATH)


# ── извлечение РАНО-идей из протокола воронки ────────────────────────────────────────
def extract_early_ideas(protocol):
    """РАНО-вердикты тайминга из протокола прогона (этап 3, пер-кандидатные вердикты §6).

    Возвращает список {актив, направление, тайминг='РАНО'}. Структурного уровня в протоколе нет
    (агент своевременности даёт текст «войти, когда Y», в протоколе сохранён лишь вердикт) — поэтому
    ставим как manual_check; оператор привяжет уровень командой CLI. П8: уровень не выдумываем.
    """
    out = []
    s3 = (protocol or {}).get("этап3_грубый_фильтр") or {}
    for v in s3.get("пер_кандидатные_вердикты") or []:
        if str(v.get("тайминг", "")).upper() == "РАНО":
            out.append({"актив": v.get("актив"), "направление": v.get("направление"),
                        "тайминг": "РАНО"})
    return out


def ingest_protocol(protocol, path=None, existing_ids=None):
    """Поставить РАНО-идеи прогона в лист ожидания (дедуп по контентному id и existing_ids).
    Возвращает список реально добавленных записей."""
    path = path or WATCHLIST_PATH
    run_id = (protocol or {}).get("run_id", "?")
    existing = set(existing_ids or [])
    already = set(current_entries(path).keys()) | existing
    added = []
    for idea in extract_early_ideas(protocol):
        asset = idea["актив"]
        source = f"funnel:{run_id}"
        text = f"РАНО (§6): войти, когда подтвердится триггер по {asset}"
        eid = _entry_id(source, asset, text)
        if eid in already:
            continue
        rec = add_entry(asset=asset, direction=idea.get("направление"),
                        trigger_text=text, trigger=None, source=source, path=path)
        already.add(rec["id"])
        added.append(rec)
    return added


# ── авто-проверка структурных триггеров по oracle.db ─────────────────────────────────
def latest_close(symbol, db_path=None):
    """Последний (date, close) по символу из oracle.db. None, если данных нет."""
    p = pathlib.Path(db_path or DB_PATH)
    if not p.exists():
        return None
    con = sqlite3.connect(str(p))
    try:
        row = con.execute(
            "SELECT date, close FROM quotes WHERE symbol=? AND close IS NOT NULL "
            "ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    return {"date": row[0], "close": float(row[1])}


def trigger_met(trigger, close):
    """Сработал ли структурный ценовой триггер при текущем close."""
    if not trigger or trigger.get("type") != "price_cross" or close is None:
        return False
    level, d = trigger.get("level"), trigger.get("dir")
    if level is None:
        return False
    if d == "above":
        return close >= level
    if d == "below":
        return close <= level
    return False


def evaluate(path=None, db_path=None, already_fired=None):
    """Проверить armed-записи со структурным триггером. Возвращает список сработавших
    {entry, observed} (без записи событий — это делает вызывающий, чтобы держать дедуп в одном месте).

    already_fired — id, по которым алерт уже отправляли (бот хранит в bot_state), повторно не дёргаем.
    """
    path = path or WATCHLIST_PATH
    db_path = db_path or DB_PATH
    fired_set = set(already_fired or [])
    out = []
    for eid, e in current_entries(path).items():
        if eid in fired_set:
            continue
        trig = e.get("trigger")
        if not trig:
            continue  # manual_check — авто-алерта нет (П8)
        lc = latest_close(trig.get("symbol"), db_path)
        if lc and trigger_met(trig, lc["close"]):
            out.append({"entry": e, "observed": lc})
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────────────
def _cli(argv=None):
    ap = argparse.ArgumentParser(description="Лист ожидания триггеров (§17, агент своевременности).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="поставить идею в лист ожидания")
    a.add_argument("--asset", required=True, help="актив тезиса, напр. Brent")
    a.add_argument("--direction", help="long/short (справочно)")
    a.add_argument("--symbol", help="символ oracle.db для структурного триггера, напр. BNO.US")
    a.add_argument("--level", type=float, help="ценовой уровень триггера")
    a.add_argument("--dir", choices=TRIGGER_DIRS, help="пересечение уровня: above/below")
    a.add_argument("--text", help="свободный текст «войти, когда Y» (если без структурного триггера)")

    sub.add_parser("list", help="показать armed-записи")

    c = sub.add_parser("cancel", help="снять запись по id")
    c.add_argument("id")

    args = ap.parse_args(argv)

    if args.cmd == "add":
        trigger = None
        if args.symbol or args.level is not None or args.dir:
            if not (args.symbol and args.level is not None and args.dir):
                ap.error("для структурного триггера нужны --symbol, --level и --dir вместе")
            trigger = make_trigger(args.symbol, args.level, args.dir)
        rec = add_entry(asset=args.asset, direction=args.direction,
                        trigger_text=args.text, trigger=trigger, source="cli")
        kind = "структурный триггер" if trigger else "ручная проверка (П8: без авто-алерта)"
        print(f"добавлено [{rec['id']}] {rec['asset']} · {kind}")
        return 0

    if args.cmd == "list":
        entries = current_entries()
        if not entries:
            print("лист ожидания пуст")
            return 0
        for eid, e in entries.items():
            t = e.get("trigger")
            cond = (f"{t['symbol']} {t['dir']} {t['level']}" if t else "ручная проверка")
            print(f"[{eid}] {e.get('asset')} · {cond} · {e.get('trigger_text') or ''}")
        return 0

    if args.cmd == "cancel":
        cancel_entry(args.id)
        print(f"снято [{args.id}]")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
