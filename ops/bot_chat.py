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
MAX_CONTEXT_CHARS = 4200     # потолок «брифа состояния» (вмещает суть выданных идей), но не жечь токены
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
    "т.п.), из чата ты этого пока не выполняешь, но можешь подсказать, как и что.\n\n"
    "ВЫДАННЫЕ ИДЕИ: в состоянии системы тебе подаётся список выданных пользователю идей с их "
    "сутью (тезис, каскад, что неизвестно, уверенность). Если пользователь спрашивает про "
    "конкретную идею (по тикеру или названию) И она есть в этом списке — обсуждай ИМЕННО её по "
    "поданным данным, НЕ говори «нет данных» и не путай с другим активом. Если идеи в списке нет "
    "— честно скажи, что в активных идеях её сейчас не видишь (могла быть в более раннем прогоне), "
    "и предложи назвать тикер/прислать карточку.\n"
    "ВАЖНО про СПОР: ты — Дирижёр ОДНОГО семейства моделей; твоё мнение НЕ заменяет состязательный "
    "суд (инвариант П10). Если пользователь СОМНЕВАЕТСЯ в идее или хочет её оспорить — не защищай и "
    "не топи её сам, а предложи запустить состязательный разбор командой «/debate <его возражение>»: "
    "там одна модель защищает идею, другая атакует его возражением, слепой судья другого семейства "
    "выносит вердикт. Кратко поясни суть идеи и её известные пробелы — но вердикт оставь контуру.\n"
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

    # Последний БОЕВОЙ прогон воронки (live; mock-фикстуры не выдаём за реальный прогон).
    try:
        import bot_reports as R
        protos = R.scan_protocols()
        live = [p for p in protos if p.get("mode") == "live"]
        if live:
            last = live[-1]
            ideas = R.ideas_from_protocol(last)
            tag = f"{last.get('run_id')} (тема {last.get('theme')})"
            if ideas:
                names = ", ".join(f"{i.get('актив')} {i.get('направление')}" for i in ideas)
                lines.append(f"Последний боевой прогон {tag}: выдано идей {len(ideas)} — {names}.")
            else:
                lines.append(f"Последний боевой прогон {tag}: идей нет — слабый день (§6).")
        elif protos:
            lines.append("Боевых прогонов пока нет — есть только тестовые (mock) прогоны на "
                         "заглушках; содержательных идей по ним делать нельзя.")
    except Exception:
        pass

    # ВЫДАННЫЕ тебе идеи, ждущие решения (из состояния бота) — с сутью каждой (П8): чтобы
    # Дирижёр обсуждал ИМЕННО пушнутые карточки, а не «последний прогон». Это и закрывает баг,
    # из-за которого чат не видел, например, CLF.US.
    issued = pending_ideas()
    if issued:
        lines.append(f"\nВЫДАННЫЕ ИДЕИ, ждущие твоего решения ({len(issued)}):")
        for it in issued:
            br = it.get("idea_brief") or {}
            p = br.get("вероятность")
            ptxt = f", уверенность ~{round(p * 100)}%" if isinstance(p, (int, float)) else ""
            head = f"• {it.get('asset')} {br.get('направление') or it.get('direction') or ''}{ptxt}"
            lines.append(head.rstrip())
            if br.get("тезис"):
                lines.append(f"    тезис: {br['тезис']}")
            if br.get("каскад"):
                lines.append(f"    каскад: {br['каскад']}")
            if br.get("что_неизвестно"):
                lines.append(f"    что неизвестно: {br['что_неизвестно']}")

    brief = "\n".join(lines).strip()
    return brief[:MAX_CONTEXT_CHARS] if brief else "Свежих данных в журналах нет."


def pending_ideas():
    """Выданные идеи, ждущие решения (из journal/bot_state.json) — с сохранённой сутью (idea_brief).

    Берём из состояния бота, а не из протоколов: пушнутая карточка должна оставаться доступной
    чату даже после ротации файлов протоколов. Старые идеи (до сохранения idea_brief) — без сути,
    но хотя бы с активом/направлением."""
    try:
        import bot_state as S
        st = S.load_state()
    except Exception:
        return []
    pend = [it for it in (st.get("pending") or {}).values() if it.get("status") == "pending"]
    pend.sort(key=lambda x: str(x.get("issued_at") or ""))
    # дедуп по активу — последняя выданная карточка по каждому активу (один и тот же тикер
    # выдаётся в разных прогонах; в чате не нужны дубли).
    by_asset = {}
    for it in pend:
        by_asset[it.get("asset")] = it
    out = list(by_asset.values())
    out.sort(key=lambda x: str(x.get("issued_at") or ""), reverse=True)
    out = out[:8]
    _backfill_briefs(out)
    return out


def _backfill_briefs(items):
    """Старым pending (до сохранения idea_brief) восстанавливаем суть из протокола-источника,
    чтобы Дирижёр обсуждал их предметно. Молча пропускаем, если файл протокола недоступен."""
    try:
        import bot_reports as R
    except Exception:
        return
    cache = {}
    for it in items:
        if it.get("idea_brief"):
            continue
        rid = it.get("run_id")
        if rid not in cache:
            cache[rid] = R.load_protocol(rid) if rid else None
        proto = cache[rid]
        if not proto:
            continue
        for card in R.ideas_from_protocol(proto):
            if card.get("актив") == it.get("asset"):
                it["idea_brief"] = R.idea_brief(card)
                break


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
