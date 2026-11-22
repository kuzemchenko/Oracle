# -*- coding: utf-8 -*-
"""mathlib/idea_score.py — балл и решение «инвестиционного комитета» по идее (Q-вердикт-комитет, владелец 22.11). Инв#6.

Вход — уже собранные кодом ответы по пунктам листа (knowledge/idea_committee.yaml): по каждому пункту
{"зачтён": True | False | None, "почему": "…"}. None = «не измерено» — в балл НЕ идёт (П8: нет факта — нет балла).
Выход — балл «X из N», решение и перечень, чего не хватает до порога ставки.

Решения (пороги — из файла листа, меняет только владелец):
  ставка    — балл ≥ порога «ставка» и нет брака          → «кандидат на маленькую ставку»;
  ожидание  — балл ≥ порога «ожидание» (или ≥ «ставка», но есть брак) → «лист ожидания» с условием;
  архив     — всё остальное, с причиной в одну строку.
Балл — НЕ вероятность: он говорит, сколько проверок пройдено с доказательством, а не «с каким шансом вырастет».
"""

BET, WAIT, ARCHIVE = "ставка", "ожидание", "архив"
DECISION_RU = {BET: "кандидат на маленькую ставку", WAIT: "лист ожидания", ARCHIVE: "архив"}


def asymmetry(entry, target, stop, direction="вверх"):
    """Возможный выигрыш / возможная потеря от цены входа. → (отношение | None, выигрыш_%, потеря_%)."""
    try:
        e, t, s = float(entry), float(target), float(stop)
    except (TypeError, ValueError):
        return None, None, None
    if e <= 0:
        return None, None, None
    up, dn = (t / e - 1, 1 - s / e) if direction == "вверх" else (1 - t / e, s / e - 1)
    if up <= 0 or dn <= 0:
        return None, round(up * 100, 2), round(dn * 100, 2)
    return round(up / dn, 2), round(up * 100, 2), round(dn * 100, 2)


def score(points, rules, blockers=()):
    """points — {id пункта: {"зачтён": bool|None, "почему": str}}; rules — загруженный лист; blockers — брак
    (ложный факт, неторгуемо, нет правила выхода, нельзя честно сверить) — с ним ставка невозможна.
    → {"балл", "из", "решение", "решение_словами", "пункты": [...], "не_хватает": [...], "брак": [...]}"""
    items = rules.get("пункты") or []
    dec = rules.get("решения") or {}
    bet_thr, wait_thr = int(dec.get("ставка", 8)), int(dec.get("ожидание", 6))
    out, n = [], 0
    for it in items:
        p = (points or {}).get(it["id"]) or {}
        ok = p.get("зачтён")
        ok = True if ok is True else (None if ok is None else False)
        n += ok is True
        out.append({"id": it["id"], "код": it.get("код"), "группа": it.get("группа"), "словами": it.get("словами"),
                    "зачтён": ok, "почему": str(p.get("почему") or ("" if ok else it.get("не_зачтён") or ""))})
    blockers = [str(b) for b in blockers or [] if str(b).strip()]
    if n >= bet_thr and not blockers:
        d = BET
    elif n >= wait_thr:
        d = WAIT
    else:
        d = ARCHIVE
    missing = [x for x in out if x["зачтён"] is not True]
    return {"балл": n, "из": len(items), "решение": d, "решение_словами": DECISION_RU[d], "пункты": out,
            "не_хватает": missing, "до_ставки": max(0, bet_thr - n), "брак": blockers,
            "пороги": {"ставка": bet_thr, "ожидание": wait_thr}}


def band(n, rules):
    """Группа балла для счёта метода: 'ставка' | 'ожидание' | 'архив' (только по баллу, без брака)."""
    dec = (rules or {}).get("решения") or {}
    if n >= int(dec.get("ставка", 8)):
        return BET
    return WAIT if n >= int(dec.get("ожидание", 6)) else ARCHIVE
