# -*- coding: utf-8 -*-
"""orchestrator/funnel.py — воронка §6 и сбор поля суждений Дирижёра (§5).

Этот модуль = Дирижёр-без-мнения (§5): он маршрутизирует агентов, собирает их суждения в
СТАНДАРТНОЕ поле суждений (§5.2), строит карту противоречий (§5.4), считает
КОНТРФАКТИЧЕСКИЙ протокол для абляции (§11.1) и накладывает процедурное вето (§5.6: нет
источников / нарушение П8). Он НЕ оценивает рынок сам.

Гейт Недели 5–6 (§24): «Сквозной прогон — все школы выдвигают кандидатов, поле суждений
собирается в стандартном формате». Состязательный контур (генератор↔критик↔судья),
экстремизация с поправкой на корреляцию и судейская вероятность — Неделя 7 (помечено TODO).
"""
import json
import math
import pathlib
import datetime
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"

import sys
sys.path.insert(0, str(ROOT))
from agents.registry import AGENTS, schools as school_specs  # noqa: E402
from orchestrator import agents as A                          # noqa: E402
from orchestrator import context as C                         # noqa: E402
from orchestrator import openrouter as OR                     # noqa: E402


def _now_compact():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Поле суждений в стандартном формате §5.2 ─────────────────────────────────────
def judgment_field(records):
    """Из записей агентов собирает поле суждений §5.2: вывод + вероятность + уверенность +
    данные-основания + что неизвестно (для каждого агента, единый формат)."""
    field = []
    for r in records:
        row = {
            "agent": r["agent"], "title": r["title"], "block": r["block"],
            "is_school": r["agent"] in {s[0] for s in school_specs()},
            "model": r.get("model"), "model_role": r["model_role"],
            "output_kind": r["output_kind"], "ok": r.get("ok", False),
        }
        if r.get("ok"):
            j = r["judgment"]
            row.update({
                "вывод": j.get("вывод"),
                "вероятность": j.get("вероятность"),
                "уверенность": j.get("уверенность"),
                "данные_основания": j.get("данные_основания", []),
                "что_неизвестно": j.get("что_неизвестно", []),
                "p8_clean": r.get("p8_clean"),
                "p8_violations": r.get("p8_violations", []),
                "no_data": r.get("no_data", False),
            })
            # видоспецифичные хвосты — тоже в поле, но не ломают единый каркас
            for extra in ("кандидаты", "вердикт", "балл", "переворот", "отыграно_pct"):
                if extra in j:
                    row[extra] = j[extra]
        else:
            row.update({"вывод": None, "ошибка": r.get("error"), "stage": r.get("stage")})
        field.append(row)
    return field


# ── Кандидаты школ (§6 этап 2) ───────────────────────────────────────────────────
def collect_candidates(records):
    school_ids = {s[0] for s in school_specs()}
    cands = []
    for r in records:
        if not r.get("ok") or r["agent"] not in school_ids:
            continue
        j = r["judgment"]
        for c in j.get("кандидаты", []) or []:
            cands.append({
                "школа": r["agent"], "актив": c.get("актив"),
                "направление": str(c.get("направление", "")).strip().lower(),
                "тезис": c.get("тезис"), "горизонт": c.get("горизонт"),
                "разрешимость": c.get("разрешимость"),
                "вероятность_школы": j.get("вероятность"),
            })
    return cands


# ── Карта противоречий (§5.4) ────────────────────────────────────────────────────
def contradiction_map(candidates):
    """Расхождения НЕ усредняются молча: один актив с противоположными направлениями →
    эскалация в дебаты по точке расхождения; неразрешённое противоречие понижает уверенность."""
    by_asset = defaultdict(lambda: defaultdict(list))
    for c in candidates:
        if c["актив"]:
            by_asset[c["актив"]][c["направление"]].append(c["школа"])
    contradictions = []
    for asset, dirs in by_asset.items():
        if "лонг" in dirs and "шорт" in dirs:
            contradictions.append({
                "актив": asset,
                "лонг": dirs["лонг"], "шорт": dirs["шорт"],
                "эскалация": "дебаты по точке расхождения (§5.4)",
            })
    return contradictions


# ── Контрфактический протокол абляции (§11.1) ────────────────────────────────────
def counterfactual_protocol(records):
    """Дирижёр на синтезе сохраняет, какой была бы агрегированная вероятность БЕЗ голоса
    каждого агента (drop-one пересчёт — дёшево, это математика, не повторные LLM-вызовы).
    Сырьё для ежемесячной абляции (§11.1): улучшал голос Brier итога или ухудшал."""
    voters = [(r["agent"], r["judgment"]["вероятность"])
              for r in records
              if r.get("ok") and r.get("judgment", {}).get("вероятность") is not None
              and r.get("p8_clean")]
    probs = [p for _, p in voters]
    n = len(probs)
    aggregate = round(sum(probs) / n, 4) if n else None
    counterfactuals = []
    for agent, p in voters:
        rest = [q for a, q in voters if a != agent]
        cf = round(sum(rest) / len(rest), 4) if rest else None
        counterfactuals.append({
            "без_агента": agent,
            "вероятность_без_него": cf,
            "сдвиг": round(cf - aggregate, 4) if (cf is not None and aggregate is not None) else None,
        })
    return {
        "метод": "drop-one среднее чистых (p8) голосов с вероятностью; экстремизация §5.5 — Неделя 7",
        "n_голосов": n,
        "агрегированная_вероятность": aggregate,
        "контрфакты": counterfactuals,
    }


# ── Процедурное вето (§5.6) ──────────────────────────────────────────────────────
def procedural_veto(records):
    flags = []
    for r in records:
        if not r.get("ok"):
            flags.append({"agent": r["agent"], "причина": "агент не дал валидного суждения",
                          "деталь": r.get("error"), "stage": r.get("stage")})
        elif not r.get("p8_clean"):
            flags.append({"agent": r["agent"], "причина": "нарушение П8 (источники/основания)",
                          "деталь": r.get("p8_violations")})
    return flags


# ── Прогон воронки ───────────────────────────────────────────────────────────────
def run_funnel(theme="brent", mode="auto", agent_ids=None, run_id=None, write=True):
    """Сквозной прогон: контекст → все агенты B/C/D/G → поле суждений + протокол Дирижёра.

    mode: 'live' (OpenRouter) | 'mock' (без сети/трат) | 'auto'.
    Возвращает dict-протокол; при write=True пишет journal/funnel_logs/{run_id}.{json,md}.
    """
    run_id = run_id or f"funnel_{_now_compact()}"
    ctx = C.build_context(theme=theme)
    client = OR.make_client(mode=mode, run_id=run_id)

    ids = agent_ids or [a[0] for a in AGENTS]
    records = [A.call_agent(aid, ctx, client) for aid in ids]

    field = judgment_field(records)
    candidates = collect_candidates(records)
    contradictions = contradiction_map(candidates)
    counterfactual = counterfactual_protocol(records)
    veto = procedural_veto(records)

    school_ids = {s[0] for s in school_specs()}
    schools_ran = [r for r in records if r["agent"] in school_ids]
    schools_ok = [r for r in schools_ran if r.get("ok")]
    schools_with_cands = {c["школа"] for c in candidates}

    protocol = {
        "run_id": run_id,
        "ts": _now_iso(),
        "mode": client.mode,
        "theme": theme,
        "spec_ref": "§5 Дирижёр, §6 воронка, §11.1 абляция; гейт Нед.5–6",
        "asof_data": {s: ctx["quotes"].get(s, {}).get("last_date") for s in ctx["quotes"]},
        "data_gaps": ctx["data_gaps"],
        "agents_total": len(records),
        "agents_ok": sum(1 for r in records if r.get("ok")),
        "schools_total": len(schools_ran),
        "schools_ok": len(schools_ok),
        "schools_with_candidates": sorted(schools_with_cands),
        "candidates_count": len(candidates),
        "поле_суждений": field,
        "кандидаты": candidates,
        "карта_противоречий": contradictions,
        "контрфактический_протокол": counterfactual,
        "процедурное_вето": veto,
        "уверенность_итога": ("понижена: есть неразрешённые противоречия" if contradictions
                              else "без эскалаций противоречий на этом прогоне"),
        "следующий_шаг": "Неделя 7: состязательный контур (слепой судья, рандомизация, рубрика), "
                         "экстремизация §5.5, риск-агент, портфель, скоринг §7",
    }
    if write:
        _write_protocol(protocol)
    return protocol


def _write_protocol(p):
    FUNNEL_LOGS.mkdir(parents=True, exist_ok=True)
    jpath = FUNNEL_LOGS / f"{p['run_id']}.json"
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    mpath = FUNNEL_LOGS / f"{p['run_id']}.md"
    mpath.write_text(_render_md(p), encoding="utf-8")
    return jpath, mpath


def _render_md(p):
    L = []
    L.append(f"# Протокол прогона воронки · {p['run_id']}")
    L.append(f"- Время: {p['ts']} · режим: **{p['mode']}** · тема: {p['theme']}")
    L.append(f"- Спецификация: {p['spec_ref']}")
    L.append(f"- Агентов: {p['agents_ok']}/{p['agents_total']} ок · "
             f"школ: {p['schools_ok']}/{p['schools_total']} · "
             f"кандидатов: {p['candidates_count']}")
    L.append("")
    L.append("## Поле суждений (стандартный формат §5.2)")
    L.append("| Агент | Блок | Школа | Модель | Вывод | P | Увер. | П8 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in p["поле_суждений"]:
        prob = "—" if r.get("вероятность") is None else f"{r['вероятность']:.2f}"
        p8 = "—"
        if r.get("ok"):
            p8 = "✓" if r.get("p8_clean") else "✗"
        vyvod = (r.get("вывод") or r.get("ошибка") or "—")
        vyvod = str(vyvod)[:48].replace("|", "/")
        L.append(f"| {r['agent']} | {r['block']} | {'да' if r['is_school'] else '—'} | "
                 f"{r.get('model','—')} | {vyvod} | {prob} | {r.get('уверенность','—')} | {p8} |")
    L.append("")
    cf = p["контрфактический_протокол"]
    L.append("## Контрфактический протокол (абляция §11.1)")
    L.append(f"- Агрегированная вероятность: **{cf['агрегированная_вероятность']}** "
             f"по {cf['n_голосов']} чистым голосам ({cf['метод']})")
    if cf["контрфакты"]:
        L.append("| Без агента | P без него | Сдвиг |")
        L.append("|---|---|---|")
        for c in cf["контрфакты"]:
            L.append(f"| {c['без_агента']} | {c['вероятность_без_него']} | {c['сдвиг']} |")
    L.append("")
    L.append("## Карта противоречий (§5.4)")
    if p["карта_противоречий"]:
        for c in p["карта_противоречий"]:
            L.append(f"- **{c['актив']}**: лонг {c['лонг']} ↔ шорт {c['шорт']} → {c['эскалация']}")
    else:
        L.append("- противоречий направлений по активам не обнаружено")
    L.append("")
    L.append("## Процедурное вето (§5.6)")
    if p["процедурное_вето"]:
        for v in p["процедурное_вето"]:
            L.append(f"- {v['agent']}: {v['причина']} — {v.get('деталь')}")
    else:
        L.append("- нарушений процедуры/П8 не зафиксировано")
    L.append("")
    L.append("## Честные пробелы данных (П8)")
    for g in p["data_gaps"]:
        L.append(f"- {g}")
    L.append("")
    L.append(f"> Следующий шаг: {p['следующий_шаг']}")
    return "\n".join(L) + "\n"
