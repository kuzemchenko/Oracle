# -*- coding: utf-8 -*-
"""ops/hunter_court.py — СУД РАЗНЫХ СЕМЕЙСТВ над идеей облачного охотника (П10).

Ревью архитектуры v3 (02.11, блокер): в облаке генератор, суд и автор охотника — одно семейство
(Claude), а идея охотника стала главным пушем дня. Межсемейный суд обязан стоять ДО доставки.
Контур: облачный охотник = ГЕНЕРАТОР (anthropic) → на сервере КРИТИК (роль critic, семейство
не anthropic) → СЛЕПОЙ СУДЬЯ (роль judge, семейство ни генератора, ни критика), позиции
«защита/критика» в детерминированно-перемешанном порядке, без авторов, по рубрике планки
(agents/prompts/hunter_court.md). Судья проверяет ЛОГИКУ карточки (механизм, канал к деньгам,
знак, прецедент того же механизма, аргумент про продавца, ответ на риск) — факты с URL и числа
до него уже проверили факт-чекер охотника и шлагбаум кодом (ops/idea_gate.py) на сервере.

Вердикт: PASS / FIX (идея уходит, сомнение — в текст строкой «вторая проверка») / FATAL (идея
не уходит, печатается тенью). Суд недоступен (нет ключа, бюджет, сбой) → (None, причина):
бот НЕ шлёт непроверенное (fail-closed по П10) и повторит позже.
Бюджет: RB.precheck("hunter_court") + суб-потолок + стоп-на-лету (Инв#5).
"""
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROMPT = ROOT / "agents" / "prompts" / "hunter_court.md"
GENERATOR_FAMILY = "anthropic"          # облачный охотник — Claude
VERDICTS = ("PASS", "FIX", "FATAL")


def _card_text(card):
    keep = ("ticker", "name", "role", "direction", "mechanism_id", "catalyst", "thesis", "facts",
            "precedent", "move_since_trigger_pct", "upside_pct", "downside_pct", "why_not_priced",
            "who_sells", "kill_conditions", "entry")
    return json.dumps({k: card.get(k) for k in keep if k in card}, ensure_ascii=False, indent=1)


def _defense(card):
    return (f"Тезис: {card.get('thesis')}\nПочему не в цене: {card.get('why_not_priced')}\n"
            f"Кто продаёт и почему ошибается: {card.get('who_sells')}\n"
            f"Что опровергнет: {json.dumps(card.get('kill_conditions'), ensure_ascii=False)}")


def parse_verdict(text):
    """Первый JSON-объект ответа → нормализованный вердикт или None (П8: мусор не голос)."""
    m = re.search(r"\{.*\}", str(text or ""), re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    verdict = str(v.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        return None
    if verdict == "FATAL" and not str(v.get("evidence") or "").strip():
        verdict = "FIX"                    # FATAL без улики — не казнь, а запрос (правила суда)
    return {"verdict": verdict, "point": v.get("point"), "evidence": str(v.get("evidence") or ""),
            "objection": str(v.get("objection") or ""), "answer": str(v.get("answer") or "")}


def judge_card(card, *, client=None, mode="auto", run_id=None):
    """Карточка → (вердикт судьи dict | None, причина | None). Критик и судья — разные семейства."""
    try:
        system = PROMPT.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"нет промпта суда: {e}"
    from orchestrator import run_budget as RB
    try:
        if client is None:
            from orchestrator.openrouter import make_client
            client = make_client(mode=mode, run_id=run_id or "hunter_court")
        if getattr(client, "mode", None) != "live" and not getattr(client, "is_stub", False):
            return None, "суд разных семейств недоступен (нет живого клиента моделей)"
        if getattr(client, "mode", None) == "live":
            decision = RB.precheck("hunter_court")
            if not decision.get("allowed"):
                return None, f"бюджет §24: {decision.get('reason') or decision}"
            client.cost_guard = RB.RunBudgetGuard("hunter_court", decision["cap_usd"])
        card_txt = _card_text(card)
        try:
            crit = client.complete("critic", system, "РОЛЬ: КРИТИК.\n\nКАРТОЧКА:\n" + card_txt,
                                   agent_id="hunter_critic", output_kind="hunter_court",
                                   exclude_family={GENERATOR_FAMILY})
            crit_txt = crit.get("text") if isinstance(crit, dict) else str(crit or "")
            crit_v = parse_verdict(crit_txt)
            if crit_v is None:
                return None, "критик дал невалидный ответ — суд не состоялся (П10)"
            excl = {GENERATOR_FAMILY}
            try:
                from orchestrator.openrouter import family_of
                fam = family_of(crit.get("model")) if isinstance(crit, dict) else None
                if fam:
                    excl.add(fam)
            except Exception:                              # noqa: BLE001
                pass
            defense = _defense(card)
            critique = f"{crit_v['objection']}" + (f" (опора: {crit_v['evidence']})" if crit_v["evidence"] else "")
            # слепота и порядок: позиции без авторов, порядок — детерминированно от карточки
            flip = int(hashlib.sha1(card_txt.encode("utf-8")).hexdigest(), 16) % 2 == 1
            a, b = (critique, defense) if flip else (defense, critique)
            user = (f"РОЛЬ: СУДЬЯ.\n\nКАРТОЧКА:\n{card_txt}\n\nПОЗИЦИЯ A:\n{a}\n\nПОЗИЦИЯ B:\n{b}")
            jr = client.complete("judge", system, user, agent_id="hunter_judge",
                                 output_kind="hunter_court", exclude_family=excl)
        except RB.RunBudgetExceeded as e:                  # BaseException — отдельная ветка (Инв#5)
            return None, f"ОСТАНОВ бюджета §24: {e.spent_usd:.4f} при потолке {e.cap_usd}"
        jv = parse_verdict(jr.get("text") if isinstance(jr, dict) else jr)
        if jv is None:
            return None, "судья дал невалидный ответ — суд не состоялся (П10)"
        jv["critic"] = crit_v
        jv["models"] = {"critic": crit.get("model") if isinstance(crit, dict) else None,
                        "judge": jr.get("model") if isinstance(jr, dict) else None}
        return jv, None
    except Exception as e:                                 # noqa: BLE001 — суд fail-closed, не падение бота
        return None, f"суд недоступен: {type(e).__name__}: {str(e)[:120]}"


def second_check_line(v):
    """FIX → строка в пуш (до рамки): сомнение второй проверки простыми словами, без вердикта-приговора."""
    obj = (v or {}).get("objection") or ""
    if not obj:
        return None
    obj = re.sub(r"\s+", " ", obj).strip().rstrip(".")
    return (f"Вторая проверка (модели других разработчиков, вслепую): главное сомнение — «{obj}». "
            "Проверю его к ближайшей дате из «Что меня опровергнет».")
