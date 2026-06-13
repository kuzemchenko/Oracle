# -*- coding: utf-8 -*-
"""ops/bot_reports.py — рендер для бота-пульта: пуш отчётов (13 полей §8), inline-клавиатура
решений, утренняя строка бюджета, алерты бюджета и срабатываний триггеров.

Чистый слой форматирования + скан артефактов (journal/funnel_logs/*.json). Сетевого I/O нет —
его делает bot.py. Так логика тестируется без Telegram.
"""
import re
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


# ── рендер отчёта: колонка, а не дамп словаря ────────────────────────────────────────
# Перевод тикера/макро-драйвера в человеческое слово для заголовка (мягкий фолбек на тикер).
_DRIVER_RU = {
    "copper": "медь", "oil": "нефть", "brent": "нефть Brent", "wti": "нефть WTI",
    "gold": "золото", "silver": "серебро", "gas": "газ", "natgas": "газ",
    "equities": "рынок акций", "spy": "рынок акций", "rates": "ставки",
    "wheat": "пшеница", "grain": "зерно", "uranium": "уран", "lithium": "литий",
}


def _humanize(v, max_n=MAX_FIELD):
    """Плоский читаемый текст из строки/списка/словаря (без JSON-скобок)."""
    if v is None:
        return ""
    if isinstance(v, dict):
        v = "; ".join(f"{k}: {_humanize(val, 120)}" for k, val in v.items())
    elif isinstance(v, list):
        v = "; ".join(_humanize(x, 160) for x in v if x not in (None, ""))
    return _trunc(v, max_n)


def _field(fields, n):
    """Значение поля §8 по его НОМЕРУ — ключи варьируются ('2_каскадная…' / '2. Каскадная…')."""
    for k, v in (fields or {}).items():
        m = re.match(r"\s*(\d+)", str(k))
        if m and int(m.group(1)) == n:
            return v
    return None


# Человеческие лейблы 13 полей §8 (вместо «9_балл_скоринга_разбивка_и_позиции_критика_судьи»).
_FIELD_LABELS = {
    1: "Актив, направление, инструмент",
    2: "Каскадная цепочка (почему именно это)",
    3: "Вероятность и калибровка",
    4: "Сценарии и асимметрия (после издержек)",
    5: "Отыгранность и стадия входа",
    6: "Кто продаёт нам — и почему он неправ",
    7: "Манипуляции и поведение толпы",
    8: "Как балансировать риск",
    9: "Скоринг по критериям + критик и судья",
    10: "Источники и их надёжность",
    11: "Что неизвестно",
    12: "Когда идея неверна (инвалидация)",
    13: "Рамка",
}


def _cascade_line(v):
    """Каскадную цепочку (список звеньев) показываем как 'триггер → … → актив'."""
    if isinstance(v, list) and v:
        return " → ".join(_humanize(x, 120) for x in v if x not in (None, ""))
    return _humanize(v)


def format_report(protocol, idea):
    """Карточка идеи: читаемый лид (хедлайн + ПОЧЕМУ) + ПОЛНЫЕ 13 граней §8 (ничего не теряем)."""
    run_id = protocol.get("run_id", "?")
    mode = protocol.get("mode")
    asset = idea.get("актив", "?")
    direction = (idea.get("направление") or "").strip()
    score = idea.get("балл")
    pos = idea.get("позиция") if isinstance(idea.get("позиция"), dict) else {}
    fields = _fields_dict(idea)

    is_long = direction.lower().startswith("лонг") or direction.lower() in ("long", "buy")
    arrow = "📈" if is_long else "📉"
    move = "ставка на рост" if is_long else "ставка на снижение"
    driver = str(pos.get("макро_драйвер") or "").lower()
    subject = _DRIVER_RU.get(driver) or _DRIVER_RU.get(asset.split(".")[0].lower()) or asset

    # Хедлайн (подстрока «ИДЕЯ» сохранена намеренно — на неё опираются тесты/поиск).
    head = f"{arrow} ИДЕЯ ДНЯ — {subject} ({asset}): {move}"
    dek_bits = []
    if isinstance(score, (int, float)):
        dek_bits.append(f"оценка {float(score):.2f}/1.00")
    prob = pos.get("вероятность")
    if isinstance(prob, (int, float)):
        dek_bits.append(f"вероятность ~{round(prob * 100)}%")
    header = [head]
    if dek_bits:
        header.append(" · ".join(dek_bits))
    if mode and mode != "live":
        header.append("⚠️ ПРОВЕРОЧНЫЙ прогон (mock): агенты НЕ рассуждали — содержательные грани "
                      "(почему/триггер) появятся только на боевом прогоне.")

    # 🎯 ПОЧЕМУ — короткий читаемый лид из каскадной цепочки (поле 2): «триггер → … → актив».
    why = None
    chain = _cascade_line(_field(fields, 2))
    if chain:
        why = f"🎯 ПОЧЕМУ {asset}: {chain}."

    # 📋 Все 13 граней §8 — полностью, с человеческими лейблами и без JSON-скобок.
    grani = ["📋 Все 13 граней (§8):"]
    for n in range(1, 14):
        label = _FIELD_LABELS.get(n, f"поле {n}")
        raw = _field(fields, n)
        v = _cascade_line(raw) if n == 2 else _humanize(raw, 360)
        grani.append(f"{n}. {label} — {v or '— нет данных'}")

    disclaimer = ("⚖️ «Оракул» — исследовательский инструмент, НЕ инвестиционная рекомендация "
                  "(§8 п.13). Решение и риск — за тобой, в рамках пре-коммитмента §12. "
                  "Кнопки решения ниже; «Принять» откроется через 24 ч (пауза §12).")
    footer = f"· прогон {run_id} · тема {protocol.get('theme') or '—'} · режим {mode}"

    parts = ["\n".join(header)]
    if why:
        parts.append(why)
    parts.append("\n".join(grani))
    text = "\n\n".join(parts) + "\n\n— — —\n" + disclaimer + "\n" + footer
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
    """Пуш слабого дня §6 человеческим языком (идей нет — это легитимный результат)."""
    fr = (protocol or {}).get("воронка_отсева") or {}
    cand = fr.get("этап2_кандидатов")
    theme = protocol.get("theme") or "—"
    sift = (f"Воронка просеяла {cand} кандидатов по теме «{theme}», но после дебатов не уцелело "
            "ни одной идеи." if isinstance(cand, int)
            else f"Воронка по теме «{theme}» не дала ни одной идеи, прошедшей фильтры и дебаты.")
    return ("🟦 Сегодня идей нет — и это нормально.\n\n"
            f"{sift} Это легитимный слабый день (§6), а не сбой: лучшее решение большинства "
            "дней — не сделать плохую ставку.\n\n"
            f"· прогон {protocol.get('run_id','?')} · режим {protocol.get('mode')}")


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
