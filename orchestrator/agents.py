# -*- coding: utf-8 -*-
"""orchestrator/agents.py — загрузка промптов и вызов одного агента.

Связывает три слоя: реестр (agents/registry) → промпт-файл (agents/prompts/{id}.md) →
клиент моделей (orchestrator/openrouter) → разбор/валидация суждения (orchestrator/judgment).
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "agents" / "prompts"

import sys
sys.path.insert(0, str(ROOT))
from agents.registry import meta as agent_meta  # noqa: E402
from orchestrator import judgment as J          # noqa: E402
from orchestrator import context as C           # noqa: E402


def load_prompt(agent_id):
    path = PROMPTS / f"{agent_id}.md"
    if not path.exists():
        raise FileNotFoundError(f"нет промпта {path} — запусти agents/build_prompts.py")
    text = path.read_text(encoding="utf-8")
    if "НУЛЕВЫЕ ВЫДУМКИ" not in text:
        raise AssertionError(f"П8 отсутствует в промпте {agent_id} (нарушение инварианта 1)")
    return text


def call_agent(agent_id, ctx, client, *, user_prompt=None, exclude_family=None):
    """Вызывает агента на срезе ctx. Возвращает запись с сырым ответом, разобранным
    суждением и нарушениями П8 (НЕ бросает на невалидном ответе — фиксирует в записи,
    чтобы протокол Дирижёра видел и брак).

    user_prompt: готовый user-текст (дело дебатов §4 блок E / синтез §8); если None —
        строится проекция широкого среза §5.5 через context.render_user_prompt.
    exclude_family: П10 — исключить семейство моделей из цепочки роли (для слепого судьи:
        семья судьи ≠ семья генератора текущей идеи).
    """
    m = agent_meta(agent_id)
    system = load_prompt(agent_id)
    user = user_prompt if user_prompt is not None else C.render_user_prompt(agent_id, ctx)

    rec = {"agent": agent_id, "title": m["title"], "block": m["block"],
           "model_role": m["model_role"], "output_kind": m["output_kind"]}
    try:
        resp = client.complete(m["model_role"], system, user,
                               agent_id=agent_id, output_kind=m["output_kind"],
                               exclude_family=exclude_family)
    except Exception as e:
        rec.update({"ok": False, "stage": "call", "error": f"{type(e).__name__}: {e}"})
        return rec

    rec.update({"model": resp.get("model"), "fallback_index": resp.get("fallback_index", 0),
                "cost_usd": resp.get("cost")})
    try:
        parsed = J.parse(resp["text"], m["output_kind"])
    except J.JudgmentError as e:
        rec.update({"ok": False, "stage": "parse", "error": str(e),
                    "raw": (resp.get("text") or "")[:600]})
        return rec

    violations = J.validate_p8(parsed)
    rec.update({"ok": True, "judgment": parsed,
                "p8_violations": violations, "p8_clean": not violations,
                "no_data": parsed.get("_no_data", False)})
    return rec
