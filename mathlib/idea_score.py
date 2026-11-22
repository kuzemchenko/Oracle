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


STRICT_MIN_N = 10            # с какого числа оценённых идей можно судить о строгости листа
NOT_COLLECTED_SHARE = 0.8    # пункт «не собирается»: данных по нему нет у такой доли оценённых идей
NOT_PASSED_SHARE = 0.5       # пункт «не проходит»: проверен и не зачтён у такой доли, и не зачтён ни разу
DEEP_MEASURED = 7            # идея «проверена глубоко»: измерено (зачтён или не прошёл) не меньше стольких пунктов
DEEP_MIN_N = 5               # о строгости листа судим по стольким глубоко проверенным идеям


def strictness(verdicts, rules, min_n=STRICT_MIN_N):
    """Не слишком ли строг лист (вопрос владельца 22.11: «ставишь мегафильтр — в результате ни одной идеи»).
    По каждому пункту считаем три исхода: зачтён / проверен и не прошёл / не измерено (данных никто не собрал).
    Строгий фильтр виден по «проверен и не прошёл»; мелкий поиск — по «не измерено». Различать обязательно:
    первое лечится правилом листа (слово владельца), второе — работой поиска.
    ПРЕДОХРАНИТЕЛЬ: о строгости судим по идеям, проверенным ГЛУБОКО (измерено ≥7 пунктов из 10). Набралось 5 таких
    и ни одна не дошла до листа ожидания → сигнал «строго»: лист обязан быть пересмотрен с владельцем.
    → {"оценено", "пункты", "лучший_балл", "дошли_до_ожидания", "дошли_до_ставки", "потолок_на_практике",
       "не_собирается", "не_проходит", "сигнал": "рано" | "норма" | "мелко" | "строго"}"""
    vs = [v for v in verdicts or [] if v.get("балл") is not None and v.get("пункты")]
    n = len(vs)
    dec = (rules or {}).get("решения") or {}
    bet_thr, wait_thr = int(dec.get("ставка", 8)), int(dec.get("ожидание", 6))
    rows = []
    for it in (rules or {}).get("пункты") or []:
        ok = bad = none = 0
        for v in vs:
            st = next((x.get("зачтён") for x in v["пункты"] if x.get("id") == it["id"]), None)
            ok += st is True
            bad += st is False
            none += st is None
        rows.append({"id": it["id"], "код": it.get("код"), "словами": it.get("словами"),
                     "зачтён": ok, "не_прошло": bad, "не_измерено": none})
    not_collected = [r for r in rows if n and r["не_измерено"] / n >= NOT_COLLECTED_SHARE]
    not_passed = [r for r in rows if n and r["зачтён"] == 0 and r["не_прошло"] / n >= NOT_PASSED_SHARE]
    best = max((v["балл"] for v in vs), default=None)
    n_wait = sum(1 for v in vs if v["балл"] >= wait_thr)
    n_bet = sum(1 for v in vs if v["балл"] >= bet_thr)
    cfg = (rules or {}).get("строгость") or {}
    deep_from, deep_min = int(cfg.get("глубокая_от", DEEP_MEASURED)), int(cfg.get("глубоких_для_вывода", DEEP_MIN_N))
    deep = [v for v in vs if sum(1 for x in v["пункты"] if x.get("зачтён") is not None) >= deep_from]
    deep_wait = sum(1 for v in deep if v["балл"] >= wait_thr)
    if n_wait:
        sig = "норма"                                   # лист проходим — идеи доходят до листа ожидания
    elif len(deep) >= deep_min:
        sig = "строго"                                  # проверили глубоко достаточно идей — и ни одна не дошла
    elif n < min_n:
        sig = "рано"
    else:
        sig = "мелко"                                   # оценённых много, а глубоко проверенных почти нет
    return {"оценено": n, "глубоких": len(deep), "глубоких_дошло": deep_wait,
            "глубокая_от": deep_from, "глубоких_для_вывода": deep_min, "пункты": rows, "лучший_балл": best, "дошли_до_ожидания": n_wait, "дошли_до_ставки": n_bet,
            "потолок_на_практике": sum(1 for r in rows if r["зачтён"] > 0), "не_собирается": not_collected,
            "не_проходит": not_passed, "сигнал": sig, "пороги": {"ставка": bet_thr, "ожидание": wait_thr, "мин_n": min_n}}

