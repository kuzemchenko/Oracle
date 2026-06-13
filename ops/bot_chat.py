# -*- coding: utf-8 -*-
"""ops/bot_chat.py — свободный диалог с Дирижёром «Оракула» прямо в Telegram.

Бот-пульт умеет не только пушить отчёты и принимать решения, но и ОТВЕЧАТЬ НА ВОПРОСЫ —
тем же «мозгом», что ведёт проект в терминале (роль conductor → Claude Opus 4.8, §26).

Дисциплина:
  • П8 — ассистент не выдумывает числа/котировки/исходы; «нет данных» — честный ответ.
  • Ответы ЗАЗЕМЛЕНЫ на живое состояние (бюджет, последний прогон, прогнозы, ворота §11),
    которое собирается из журналов и подаётся модели как контекст.
  • Стоимость каждого вызова пишет в journal/costs.jsonl сам LiveClient (бюджет §30).
  • Рамка «исследовательский инструмент, не рекомендация» — в системном промпте.

Сеть/ключ — через orchestrator.openrouter.LiveClient (OPENROUTER_API_KEY из .env). Для тестов
answer() принимает готовый client (внедрение зависимости), без сети.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "ops")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MAX_HISTORY_TURNS = 6        # сколько последних реплик пары (вопрос+ответ) держим в контексте
MAX_CONTEXT_CHARS = 2200     # потолок «брифа состояния», чтобы не жечь токены
MAX_REPLY_CHUNK = 3900       # под лимит Telegram 4096 (разбивка длинных ответов делается в bot.py)

SYSTEM_PROMPT = (
    "Ты — Дирижёр инвестиционно-аналитической системы «Оракул», и говоришь с её владельцем "
    "в Telegram. Это тот же ум, что ведёт проект в терминале (Claude Opus 4.8).\n\n"
    "СТИЛЬ: ясный, человеческий, как колонка делового журнала. Короткие абзацы, без жаргона, "
    "без дампов словарей и кода. Если уместно — короткий вывод в начале, затем объяснение.\n\n"
    "НЕРУШИМЫЕ ПРИНЦИПЫ:\n"
    "• П8 — ничего не выдумывай. Нет данных → честно «не знаю / нет данных». Не сочиняй числа, "
    "котировки, вероятности, исходы прогнозов. Опирайся только на поданное состояние системы и "
    "на устройство «Оракула».\n"
    "• Рамка: «Оракул» — персональный исследовательский инструмент, НЕ инвестиционная "
    "рекомендация. Решение и риск — за пользователем (§12). Не уговаривай купить/продать.\n\n"
    "ЧТО МОЖЕШЬ: объяснять логику системы, теории (поведенческая экономика, рефлексивность, "
    "каскады 2–4 порядка), текущее состояние, статус ворот, прогнозы, бюджет; обсуждать идеи и "
    "риски трезво. Если просят ЗАПУСТИТЬ прогон, изменить веса/лимиты или применить поправки — "
    "объясни, что это делается командами в терминале (/run-funnel, /calibrate, /apply-weights и "
    "т.п.), из чата ты этого пока не выполняешь, но можешь подсказать, как и что.\n"
    "Отвечай на русском, по делу, без воды."
)


def _read_jsonl(path):
    out = []
    p = pathlib.Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def context_brief():
    """Краткий «брифинг состояния» для заземления ответа (живые факты из журналов)."""
    lines = []

    # Бюджет токенов (§30) — без сетевого запроса ключа.
    try:
        import budget as B
        limits = B.load_budget_limits()
        tokens, _bm, _bmod = B.oracle_monthly_spend(limits["costs_log"])
        st = B.compute_status(tokens, limits)
        lines.append(f"Бюджет токенов в этом месяце: ${st['oracle_tokens_usd']:.2f} из "
                     f"${st['tokens_cap']:.0f} (статус {st['status']}).")
    except Exception:
        pass

    # Запечатанные прогнозы и ближайшая сверка (§9, §11).
    preds = _read_jsonl(ROOT / "journal" / "predictions.jsonl")
    if preds:
        resolves = sorted({p.get("resolve_by") for p in preds if p.get("resolve_by")})
        nearest = resolves[0] if resolves else "—"
        lines.append(f"Запечатанных прогнозов: {len(preds)}; ближайшая сверка исхода — {nearest}. "
                     f"До денежных ворот Б→Д нужно 270 разрешённых исходов (сейчас дозревших мало).")

    # Последний прогон воронки.
    try:
        import bot_reports as R
        protos = R.scan_protocols()
        if protos:
            last = protos[-1]
            ideas = R.ideas_from_protocol(last)
            tag = f"{last.get('run_id')} (тема {last.get('theme')}, режим {last.get('mode')})"
            if ideas:
                names = ", ".join(f"{i.get('актив')} {i.get('направление')}" for i in ideas)
                lines.append(f"Последний прогон {tag}: выдано идей {len(ideas)} — {names}.")
            else:
                lines.append(f"Последний прогон {tag}: идей нет — слабый день (§6).")
    except Exception:
        pass

    brief = "\n".join(lines).strip()
    return brief[:MAX_CONTEXT_CHARS] if brief else "Свежих данных в журналах нет."


def build_user_message(user_text, history=None):
    """Склейка: брифинг состояния + недавний диалог + вопрос. История — список {role,text}."""
    history = history or []
    parts = ["ТЕКУЩЕЕ СОСТОЯНИЕ СИСТЕМЫ (заземление; не выдумывай сверх этого):",
             context_brief()]
    recent = history[-2 * MAX_HISTORY_TURNS:]
    if recent:
        convo = "\n".join(
            f"{'Пользователь' if h.get('role') == 'user' else 'Ты'}: {h.get('text', '')}"
            for h in recent)
        parts += ["", "НЕДАВНИЙ ДИАЛОГ:", convo]
    parts += ["", "ВОПРОС ПОЛЬЗОВАТЕЛЯ:", user_text]
    return "\n".join(parts)


def answer(user_text, history=None, client=None):
    """Ответ Дирижёра. Возвращает (текст, стоимость_usd_или_None). client — для тестов без сети."""
    cli = client
    if cli is None:
        from orchestrator.openrouter import LiveClient   # ленивый импорт: тестам сеть/ключ не нужны
        cli = LiveClient(run_id="bot_chat")
    user = build_user_message(user_text, history)
    res = cli.complete("conductor", SYSTEM_PROMPT, user,
                       agent_id="bot_chat", output_kind="chat")
    return (res.get("text") or "").strip(), res.get("cost")
