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


_KEEP = ("ticker", "name", "role", "direction", "mechanism_id", "catalyst", "thesis", "facts",
         "precedent", "move_since_trigger_pct", "upside_pct", "downside_pct", "trigger_date",
         "trigger_close", "target_price", "stop_price", "entry_limit", "why_not_priced",
         "who_sells", "kill_conditions", "entry")


def _card_text(card):
    return json.dumps({k: card.get(k) for k in _KEEP if k in card}, ensure_ascii=False, indent=1)


def _norm(t):
    """Нормализация для поиска улики: регистр, кавычки любого вида, пробелы/переносы."""
    t = re.sub(r"[«»“”„\"'`]", "", str(t or "")).lower()
    return re.sub(r"\s+", " ", t).strip()


def _haystack(card):
    """Плоский текст ЗНАЧЕНИЙ карточки (без JSON-экранирования) — где ищется дословная улика
    FATAL (перепроверка №2: поиск в json.dumps разжаловал цитаты с кавычкой/переносом в FIX)."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif v is not None:
            out.append(str(v))
    walk({k: (card or {}).get(k) for k in _KEEP if k in (card or {})})
    return _norm(" ".join(out))


def _defense(card):
    return (f"Тезис: {card.get('thesis')}\nПочему не в цене: {card.get('why_not_priced')}\n"
            f"Кто продаёт и почему ошибается: {card.get('who_sells')}\n"
            f"Что опровергнет: {json.dumps(card.get('kill_conditions'), ensure_ascii=False)}")


def parse_verdict(text, card_text=None):
    """ПЕРВЫЙ JSON-объект ответа → нормализованный вердикт или None (П8: мусор не голос).
    FATAL засчитывается, только если пункт рубрики 1–4 и улика ДОСЛОВНО есть в карточке
    (перепроверка 02.11: «казнь» кодом не сверялась) — иначе это FIX."""
    t = str(text or "")
    i = t.find("{")
    if i < 0:
        return None
    try:
        v, _end = json.JSONDecoder().raw_decode(t[i:])
    except ValueError:
        return None
    if not isinstance(v, dict):
        return None
    verdict = str(v.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        return None
    ev = str(v.get("evidence") or "").strip().strip("«»\"' ")
    try:
        point = int(v.get("point") or 0)
    except (TypeError, ValueError):
        point = 0
    if verdict == "FATAL" and (not ev or point not in (1, 2, 3, 4)
                               or (card_text is not None and _norm(ev) not in _norm(card_text))):
        verdict = "FIX"                    # FATAL без проверяемой улики — не казнь, а запрос
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
            hay = _haystack(card)
            crit_v = parse_verdict(crit_txt, hay)
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
        jv = parse_verdict(jr.get("text") if isinstance(jr, dict) else jr, hay)
        if jv is None:
            return None, "судья дал невалидный ответ — суд не состоялся (П10)"
        jv["critic"] = crit_v
        jv["models"] = {"critic": crit.get("model") if isinstance(crit, dict) else None,
                        "judge": jr.get("model") if isinstance(jr, dict) else None}
        return jv, None
    except Exception as e:                                 # noqa: BLE001 — суд fail-closed, не падение бота
        return None, f"суд недоступен: {type(e).__name__}: {str(e)[:120]}"


GENERIC_LINE = ("Вторая проверка (модели других разработчиков, вслепую) нашла сомнение, которое "
                "я ещё досчитываю, — проверю его к ближайшей дате из «Что меня опровергнет».")


def safe_objection(v):
    """Возражение суда, ГОДНОЕ к показу владельцу, или None. Перепроверка №3 (03.11, блокер): сырой
    текст модели другого семейства уходил по кнопке «Качаем» и в заметки дозора в обход линта, хотя
    тот же текст для пуша отбраковывался. Единый фильтр для ВСЕХ поверхностей: вероятности/шансы
    (в т.ч. в кавычках), тех.id, больше 2 чисел, жаргон, ёлочки → None (П8/Инв#6)."""
    obj = (v or {}).get("objection") or ""
    obj = re.sub(r"\s+", " ", str(obj)).strip().rstrip(".")[:220]
    if not obj:
        return None
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    nums = re.findall(r"\d+(?:[.,]\d+)?", obj)
    if (PL.odds_markers(obj, include_quotes=True) or PL._TECH_ID_RE.search(obj) or len(nums) > 2
            or PL._unexplained_terms(obj) or "«" in obj or "»" in obj):
        return None
    return obj


def second_check_line(v):
    """FIX → строка в пуш (до рамки): сомнение второй проверки простыми словами, без приговора.
    Текст проходит safe_objection; не прошёл — нейтральная строка без содержимого и без обещаний."""
    if not (v or {}).get("objection"):
        return None
    obj = safe_objection(v)
    if obj is None:
        return GENERIC_LINE
    return (f"Вторая проверка (модели других разработчиков, вслепую): главное сомнение — «{obj}». "
            "Проверю его к ближайшей дате из «Что меня опровергнет».")
