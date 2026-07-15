# -*- coding: utf-8 -*-
"""ops/bot_state.py — состояние бота-пульта и журнал решений пользователя.

Раздел «Интерфейс»: канал человеко-решения (П12 «решение о риске за человеком»,
§12 «защита пользователя от себя»). Здесь:
  • мутабельное состояние бота (journal/bot_state.json) — что уже спушено, статусы идей,
    последний обработанный update, даты алертов. Это ВНУТРЕННЕЕ состояние бота, НЕ запечатанный
    журнал — его можно перезаписывать.
  • append-only журнал решений (journal/decisions_user.jsonl) — «принял/отклонил/отложил + мотив»
    (§12). Журнал ЗАПЕЧАТАН (хук guard_journal): только append одной строкой, ничего не удаляется
    (§16). Мотив снимается отдельным событием-аннотацией (журнал append-only — не переписываем
    строку решения, а дописываем ссылку на неё).

Пауза пре-коммитмента (§12): «медленные» идеи (каскады — основная игра, живут днями/неделями)
получают паузу ≥ суток до решения. Кнопка «Принять» разблокируется через PRECOMMIT_HOURS=24 ч
от момента ВЫДАЧИ идеи (issued_at = ts отчёта прогона). «Отклонить»/«Отложить» доступны сразу —
пауза работает только против импульсивного ПРИНЯТИЯ, не против отказа.
"""
import json
import hashlib
import datetime
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "journal" / "bot_state.json"
DECISIONS_PATH = ROOT / "journal" / "decisions_user.jsonl"

# §12: «медленные» идеи — пауза ≥ суток до решения. Зашито в код, не в обещания.
PRECOMMIT_HOURS = 24

ACTIONS = ("accept", "reject", "defer")
_ACTION_RU = {"accept": "ПРИНЯЛ", "reject": "ОТКЛОНИЛ", "defer": "ОТЛОЖИЛ"}


# ── время ────────────────────────────────────────────────────────────────────────
def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def parse_iso(s):
    """Терпимый парсер ISO-времени → aware UTC datetime (None при провале)."""
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ── ключ идеи (для callback_data ≤64 байт) ─────────────────────────────────────────
def idea_key(run_id, asset):
    """Стабильный человекочитаемый ключ идеи: '<run_id>|<asset>'."""
    return f"{run_id}|{asset}"


def idea_token(run_id, asset):
    """Короткий стабильный токен идеи для callback_data Telegram (лимит 64 байта): полный ключ
    может быть длинным/кириллическим (актив = тезис) → используем sha1[:12], а карточку идеи
    бот держит в state['pending'][token]."""
    return hashlib.sha1(idea_key(run_id, asset).encode("utf-8")).hexdigest()[:12]


# ── пауза пре-коммитмента §12 ──────────────────────────────────────────────────────
def hours_since(issued_at, now=None):
    """Сколько часов прошло с момента выдачи идеи (float). None если issued_at не распознан."""
    dt = parse_iso(issued_at)
    if dt is None:
        return None
    now = now or now_utc()
    return (now - dt).total_seconds() / 3600.0


def accept_unlock_at(issued_at):
    """Момент, когда «Принять» разблокируется (issued_at + 24ч)."""
    dt = parse_iso(issued_at)
    if dt is None:
        return None
    return dt + datetime.timedelta(hours=PRECOMMIT_HOURS)


def accept_unlocked(issued_at, now=None):
    """True, если пауза пре-коммитмента §12 выдержана и «Принять» можно нажать."""
    h = hours_since(issued_at, now)
    return h is not None and h >= PRECOMMIT_HOURS


def hours_remaining(issued_at, now=None):
    """Сколько часов ещё до разблокировки «Принять» (0.0, если уже можно)."""
    h = hours_since(issued_at, now)
    if h is None:
        return None
    return max(0.0, PRECOMMIT_HOURS - h)


# ── состояние бота (мутабельное) ───────────────────────────────────────────────────
def _default_state():
    return {
        "update_offset": 0,          # next getUpdates offset (last_update_id + 1)
        "pushed_runs": [],           # run_id отчётов, уже спушенных (дедуп пуша)
        "pending": {},               # idea_key -> карточка идеи (issued_at, статус, message_id…)
        "last_budget_line_date": None,   # дата последней утренней строки бюджета (YYYY-MM-DD)
        "last_budget_alert_date": None,  # дата последнего алерта бюджета (дедуп в сутки)
        "fired_triggers": [],        # id сработавших триггеров листа ожидания (дедуп алертов)
        "seen_watchlist": [],        # id записей watchlist, уже обработанных ботом
        "chat_history": [],          # свободный диалог с Дирижёром: [{role, text}] (обрезается)
    }


def load_state(path=None):
    p = pathlib.Path(path or STATE_PATH)
    if not p.exists():
        return _default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_state()
    st = _default_state()
    st.update(data)
    return st


def save_state(state, path=None):
    p = pathlib.Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # атомарная подмена — состояние не бьётся при падении посреди записи


# ── журнал решений §12 (append-only) ───────────────────────────────────────────────
def _append_jsonl(path, record):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_decision(*, run_id, asset, direction, score, action, issued_at,
                    chat_id, now=None, path=None):
    """Запись решения пользователя (§12: принял/отклонил/отложил). Append-only.

    precommit_ok фиксирует, была ли выдержана пауза §12 на момент принятия (для accept всегда
    True — вызывающий обязан проверить accept_unlocked ДО записи; пишем как улику честности).
    Возвращает записанную строку (для последующей мотив-аннотации).
    """
    if action not in ACTIONS:
        raise ValueError(f"action должно быть из {ACTIONS}, получено {action!r}")
    now = now or now_utc()
    h = hours_since(issued_at, now)
    rec = {
        "ts": iso(now),
        "type": "decision",
        "action": action,
        "run_id": run_id,
        "asset": asset,
        "direction": direction,
        "score": score,
        "issued_at": issued_at,
        "hours_since_issue": (round(h, 2) if h is not None else None),
        "precommit_hours": PRECOMMIT_HOURS,
        "precommit_ok": (accept_unlocked(issued_at, now) if action == "accept" else None),
        "motive": None,              # снимается отдельным событием 'motive' (append-only)
        "source": "telegram_bot",
        "chat_id": chat_id,
    }
    _append_jsonl(path or DECISIONS_PATH, rec)
    return rec


def append_motive(*, run_id, asset, action, motive, chat_id, now=None, path=None):
    """Мотив-аннотация к ранее записанному решению (§12 «мотив»). Append-only отдельной строкой —
    журнал не переписывается (§16), аннотация ссылается на решение по (run_id, asset, action)."""
    now = now or now_utc()
    rec = {
        "ts": iso(now),
        "type": "motive",
        "action": action,
        "run_id": run_id,
        "asset": asset,
        "motive": motive,
        "source": "telegram_bot",
        "chat_id": chat_id,
    }
    _append_jsonl(path or DECISIONS_PATH, rec)
    return rec


def append_case_feedback(*, run_id, asset, status, answer, chat_id, now=None, path=None):
    """Этап3: разметка владельца по «Разбору дня» — ответ на вопрос кейса (тренировка суждения).
    Append-only отдельной строкой type='case_feedback'; это НЕ ставка (§12 accept/reject/defer к нему
    не применяются) — сигнал качества выдачи для петли §25. run_id/asset/status привязывают ответ к
    показанному кейсу; answer — выбранный владельцем вариант (или свободный текст)."""
    now = now or now_utc()
    rec = {
        "ts": iso(now),
        "type": "case_feedback",
        "run_id": run_id,
        "asset": asset,
        "case_status": status,
        "answer": answer,
        "source": "telegram_bot",
        "chat_id": chat_id,
    }
    _append_jsonl(path or DECISIONS_PATH, rec)
    return rec


def action_ru(action):
    return _ACTION_RU.get(action, action)
