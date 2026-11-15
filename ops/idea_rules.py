# -*- coding: utf-8 -*-
"""ops/idea_rules.py — загрузчик единого источника правил качества идеи (knowledge/idea_rules.yaml, правила v3;
владелец 15.11: вариант «Б»). Код помечает каждое нарушение номером правила; брак это или пометка — решает файл.
Неизвестный номер — брак (fail-closed: опечатка в коде не должна тихо ослаблять проверку)."""
import datetime
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
PATH = ROOT / "knowledge" / "idea_rules.yaml"
HARD, SOFT = "брак", "пометка"


def load(path=None):
    return yaml.safe_load(pathlib.Path(path or PATH).read_text(encoding="utf-8")) or {}


def kinds(rules=None):
    r = rules if rules is not None else load()
    out = {x["id"]: HARD for x in r.get("брак") or []}
    out.update({x["id"]: SOFT for x in r.get("пометки") or []})
    return out


def kind(rule_id, rules=None):
    return kinds(rules).get(rule_id, HARD)


def active_from(rules=None):
    r = rules if rules is not None else load()
    v = r.get("действует_с")
    try:
        return datetime.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def active(hunt_date, rules=None):
    """Правила v3 в силе для охоты этой даты? (нет даты включения — нет)."""
    d = active_from(rules)
    return bool(d and hunt_date and hunt_date >= d)


def court_fatal_points(rules=None):
    r = rules if rules is not None else load()
    return tuple(int(x) for x in ((r.get("суд") or {}).get("убивает_только_пункты") or []))


def split(tagged, v3, rules=None):
    """tagged — [(номер_правила, текст)]. → (брак, пометки). Без v3 всё — брак (прежнее поведение)."""
    if not v3:
        return [m for _, m in tagged], []
    k = kinds(rules)
    hard = [m for t, m in tagged if k.get(t, HARD) == HARD]
    soft = [f"пометка ({t.lower()}): {m}" for t, m in tagged if k.get(t, HARD) == SOFT]
    return hard, soft


def weak_points(tagged, rules=None):
    """Номера-пометки из нарушений → фразы для владельца (без повторов, в порядке файла правил)."""
    r = rules if rules is not None else load()
    have = {t for t, _ in tagged or []}
    return [x["словами"] for x in r.get("пометки") or [] if x["id"] in have and x.get("словами")]


def text_for_prompt(rules=None):
    """Правила словами — для заданий моделям (суд, охотник): одна правда с кодом."""
    r = rules if rules is not None else load()
    L = ["ПРАВИЛА КАЧЕСТВА ИДЕИ (knowledge/idea_rules.yaml, v3):", "Брак — идея не уходит:"]
    L += [f"- {x['id']}: {x['текст']}" for x in r.get("брак") or []]
    L.append("Пометки — не убивают, идут в текст «что слабо»:")
    L += [f"- {x['id']}: {x['текст']}" for x in r.get("пометки") or []]
    if (r.get("суд") or {}).get("правило"):
        L.append("Суд: " + r["суд"]["правило"])
    return "\n".join(L)
