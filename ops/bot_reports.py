# -*- coding: utf-8 -*-
"""ops/bot_reports.py — рендер для бота-пульта: пуш отчётов (13 полей §8), inline-клавиатура
решений, утренняя строка бюджета, алерты бюджета и срабатываний триггеров.

Чистый слой форматирования + скан артефактов (journal/funnel_logs/*.json). Сетевого I/O нет —
его делает bot.py. Так логика тестируется без Telegram.
"""
import json
import html
import pathlib

import bot_state as S

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"

# Поля §8 в каноническом порядке (ключи — как их кладёт синтезатор; рендер терпим к отсутствию).
FIELDS_8 = [
    "1. Актив/направление/инструмент", "2. Каскадная цепочка", "3. Вероятность + калибровка",
    "4. Сценарии заработать/потерять; асимметрия", "5. Отыгранность и стадия входа",
    "6. Кто продаёт нам и почему неправ", "7. Манип-балл + поведенческий диагноз",
    "8. Балансировка риска", "9. Скоринг; критик и судья", "10. Источники с credibility",
    "11. Что неизвестно (П8)", "12. Сценарии инвалидации", "13. Рамка-дисклеймер",
]

MAX_FIELD = 320          # обрезка одного поля в пуше
MAX_MSG = 3800           # запас под лимит Telegram 4096


def _trunc(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _fields_dict(idea):
    """Достаёт словарь 13 полей §8 из карточки отчёта.

    Синтезатор оборачивает отчёт в запись вызова агента: отчёт = {agent, …, judgment: {…, поля}}.
    Терпимо ищем 'поля' на нескольких уровнях; иначе отдаём сам отчёт (рендер покажет, что есть)."""
    rep = idea.get("отчёт") or {}
    if not isinstance(rep, dict):
        return {}
    for cand in (rep.get("поля"),
                 (rep.get("judgment") or {}).get("поля") if isinstance(rep.get("judgment"), dict) else None):
        if isinstance(cand, dict) and cand:
            return cand
    return rep


# ── скан новых отчётов прогона ──────────────────────────────────────────────────────
def load_protocol(run_id, logs_dir=None):
    p = pathlib.Path(logs_dir or FUNNEL_LOGS) / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def scan_protocols(logs_dir=None):
    """Все протоколы в журнале, по возрастанию run_id (run_id несёт timestamp → сортируется)."""
    d = pathlib.Path(logs_dir or FUNNEL_LOGS)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def ideas_from_protocol(protocol):
    """Список карточек идей (отчёты этапа 6) из протокола. Пусто — слабый день §6."""
    synth = (protocol or {}).get("этап6_синтез") or {}
    return synth.get("отчёты") or []


def new_runs(pushed_run_ids, logs_dir=None):
    """Протоколы, которые бот ещё не пушил (по run_id)."""
    pushed = set(pushed_run_ids or [])
    return [p for p in scan_protocols(logs_dir or FUNNEL_LOGS) if p.get("run_id") not in pushed]


# ── рендер отчёта (13 полей §8) ─────────────────────────────────────────────────────
def format_report(protocol, idea):
    run_id = protocol.get("run_id", "?")
    asset = idea.get("актив", "?")
    direction = idea.get("направление") or ""
    score = idea.get("балл")
    pos = idea.get("позиция")
    head = f"📑 ИДЕЯ: {asset}"
    if direction:
        head += f" · {direction}"
    if isinstance(score, (int, float)):
        head += f" · балл {score:.1f}" if isinstance(score, float) else f" · балл {score}"
    lines = [head, f"прогон {run_id} · тема {protocol.get('theme') or '—'} · режим {protocol.get('mode')}"]
    if pos:
        lines.append(f"позиция: {_trunc(pos, 200)}")
    lines.append("— — —")

    fields = _fields_dict(idea)
    keys = list(fields.keys())
    canonical = keys if keys else FIELDS_8
    body = []
    for i, k in enumerate(canonical):
        v = fields.get(k)
        label = k if keys else FIELDS_8[i]
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        label = str(label).replace("_", " ")
        body.append(f"• {_trunc(label, 80)}: {_trunc(v, MAX_FIELD)}")
    if not body:
        body.append("• (синтезатор не вернул поля §8 — см. протокол прогона)")
    lines += body
    lines.append("— — —")
    lines.append("⚖️ Исследовательский инструмент, НЕ инвестиционная рекомендация (§8 п.13). "
                 "Решение о риске — за тобой, в рамках пре-коммитмента §12.")
    text = "\n".join(lines)
    if len(text) > MAX_MSG:
        text = text[:MAX_MSG].rstrip() + "\n…(обрезано; полный отчёт — в journal/funnel_logs)"
    return text


def build_keyboard(token, issued_at, now=None):
    """Inline-клавиатура решения. «Принять» заблокирована паузой §12 (24ч от выдачи).
    Заблокированная кнопка остаётся видимой (с таймером) — нажатие бот отвергнет с подсказкой."""
    unlocked = S.accept_unlocked(issued_at, now)
    if unlocked:
        accept_label = "✅ Принять"
    else:
        rem = S.hours_remaining(issued_at, now)
        rem_txt = f"{rem:.0f}ч" if rem is not None and rem >= 1 else "<1ч"
        accept_label = f"🔒 Принять (через {rem_txt})"
    return {"inline_keyboard": [[
        {"text": accept_label, "callback_data": f"d:a:{token}"},
        {"text": "❌ Отклонить", "callback_data": f"d:r:{token}"},
        {"text": "🕒 Отложить", "callback_data": f"d:p:{token}"},
    ]]}


def format_weak_day(protocol):
    """Однострочный пуш слабого дня §6 (идей нет — легитимный результат)."""
    fr = (protocol or {}).get("воронка_отсева") or {}
    return (f"🟦 Воронка {protocol.get('run_id','?')} (тема {protocol.get('theme') or '—'}): "
            f"стоящих идей нет — легитимный слабый день §6. "
            f"Кандидатов {fr.get('этап2_кандидатов','?')} → после дебатов "
            f"{fr.get('этап5_устояло_после_дебатов','?')} → выдано {fr.get('этап6_выдано_топ', 0)}.")


# ── бюджет ──────────────────────────────────────────────────────────────────────────
def format_budget_line(one_liner_text):
    """Утренняя строка бюджета §15 (обёртка над budget.one_liner)."""
    return "🌅 Утренняя строка бюджета (§15)\n" + one_liner_text


def format_budget_alert(st, one_liner_text):
    """Алерт бюджета: ВНИМАНИЕ (≥alert) или ПРЕВЫШЕНИЕ (≥100% → прогоны стоп)."""
    icon = "🛑" if st.get("exit_code") == 3 else "⚠️"
    head = ("ПРЕВЫШЕНИЕ потолка бюджета — прогоны стоп (§11/§12, лимиты в коде)"
            if st.get("exit_code") == 3 else f"Бюджет: {st.get('status')}")
    return f"{icon} {head}\n{one_liner_text}"


# ── алерт триггера листа ожидания ────────────────────────────────────────────────────
def format_trigger_alert(fired):
    """fired = {'entry':..., 'observed': {date, close}} из bot_watchlist.evaluate."""
    e = fired["entry"]
    obs = fired["observed"]
    t = e.get("trigger") or {}
    direction = e.get("direction")
    dir_txt = {"above": "≥", "below": "≤"}.get(t.get("dir"), t.get("dir"))
    head = f"🔔 Сработал триггер листа ожидания: {e.get('asset')}"
    if direction:
        head += f" · {direction}"
    return (f"{head}\n"
            f"{t.get('symbol')} close {obs.get('close')} {dir_txt} уровня {t.get('level')} "
            f"(на {obs.get('date')}).\n"
            f"Контекст: {e.get('trigger_text') or '—'}\n"
            f"Окно входа §6: после события 1-го порядка, до того как рынок свяжет цепочку. "
            f"Прогон воронки по активу для свежего отчёта.")


def escape(s):
    """На случай parse_mode=HTML (по умолчанию шлём без разметки — экранирование не требуется)."""
    return html.escape(str(s))
