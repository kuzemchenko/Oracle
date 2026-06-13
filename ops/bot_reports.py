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


def _section(title, *parts):
    """Абзац колонки: ЗАГОЛОВОК + связный текст. Каждый кусок завершается точкой, чтобы
    предложения не слипались. Пусто → None (абзац пропускается)."""
    chunks = []
    for c in parts:
        if not c or not str(c).strip():
            continue
        s = str(c).strip()
        if s[-1] not in ".!?…;:":
            s += "."
        chunks.append(s)
    if not chunks:
        return None
    return f"{title} " + " ".join(chunks)


def format_report(protocol, idea):
    """Карточка идеи в стиле колонки делового журнала: суть, расклад, риски, что делать."""
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

    # Заголовок-«хедлайн» (подстрока «ИДЕЯ» сохранена намеренно — на неё опираются тесты/поиск).
    head = f"{arrow} ИДЕЯ ДНЯ — {subject}: {move}"
    dek = f"{asset} · {direction or '—'}"
    if isinstance(score, (int, float)):
        dek += f" · оценка идеи {float(score):.2f} из 1.00"
    header = [head, dek]
    if mode and mode != "live":
        header.append("⚠️ Это ПРОВЕРОЧНЫЙ прогон (режим mock) — тест формата, не живая идея.")

    # Тело — связными абзацами по смыслу, а не «поле N: значение».
    paras = []
    paras.append(_section("СУТЬ.",
                          _humanize(_field(fields, 1)),
                          ("Каскад: " + _humanize(_field(fields, 2))) if _field(fields, 2) else ""))
    paras.append(_section("РАСКЛАД.",
                          _humanize(_field(fields, 3)),
                          _humanize(_field(fields, 4))))
    paras.append(_section("КТО ПО ДРУГУЮ СТОРОНУ.",
                          _humanize(_field(fields, 6)),
                          ("Отыграно: " + _humanize(_field(fields, 5))) if _field(fields, 5) else ""))
    risk_bits = []
    if _field(fields, 7):
        risk_bits.append(_humanize(_field(fields, 7)))
    if _field(fields, 12):
        risk_bits.append("Идея перестанет работать, если: " + _humanize(_field(fields, 12)))
    if _field(fields, 11):
        risk_bits.append("Чего пока не знаем: " + _humanize(_field(fields, 11)))
    paras.append(_section("ЧЕГО ОПАСАТЬСЯ.", *risk_bits))
    entry = _humanize(_field(fields, 8))
    size_note = ""
    amt = pos.get("amount_usd")
    if amt:
        size_note = f"Размер — микро ${float(amt):.0f} (Келли выключен до подтверждения калибровки)."
    paras.append(_section("ЕСЛИ ВХОДИТЬ.", entry, size_note))
    if _field(fields, 10):
        paras.append(_section("ИСТОЧНИКИ.", _humanize(_field(fields, 10))))

    body = [p for p in paras if p]
    if not body:
        body = ["(Синтезатор не вернул поля §8 — подробности в протоколе прогона.)"]

    disclaimer = ("⚖️ «Оракул» — исследовательский инструмент, НЕ инвестиционная рекомендация "
                  "(§8 п.13). Решение и риск — за тобой, в рамках пре-коммитмента §12. "
                  "Кнопки решения ниже; «Принять» откроется через 24 ч (пауза §12).")
    footer = f"· прогон {run_id} · тема {protocol.get('theme') or '—'} · режим {mode}"

    text = ("\n".join(header) + "\n\n— — —\n\n"
            + "\n\n".join(body)
            + "\n\n— — —\n" + disclaimer + "\n" + footer)
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
