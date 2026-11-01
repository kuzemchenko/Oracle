# -*- coding: utf-8 -*-
"""mathlib/calibration/recalib.py — V4 (карта v3): монотонная рекалибровка сырых вероятностей.

Замена константе П-1 (λ=0 → все официальные вероятности одинаковы, идеи неразличимы):
изотоническая регрессия (PAV) по СОБСТВЕННЫМ форвард-исходам трека сохраняет ранжирование
(«эта идея сильнее той») и возвращает честный УРОВЕНЬ. Подписано владельцем 01.11 (П2 пакета
подач + подпись карты v3 31.10).

Честность (уроки аудита 31.10):
  • ставки одного дня скоррелированы → доверительные интервалы и метрики считаем
    КЛАСТЕРНЫМ бутстрэпом ПО ДНЯМ, не по строкам (иначе снова самообман, p=0.071-урок);
  • walk-forward: фит на ранней части времени, отчёт о качестве — на поздней (OOS);
  • вне диапазона обучающих вероятностей — честный клип к краям, не экстраполяция;
  • детерминизм: бутстрэп на фиксированном seed (Инв#6 — числа воспроизводимы).

Применение к боевому seal — отдельный шаг со stage-review (меняет official-числа).
"""
import bisect
import json
import random


def pav_fit(probs, outcomes, weights=None):
    """Изотоническая регрессия (Pool Adjacent Violators) p_raw → p_cal.

    Возвращает список узлов [(p_raw, p_cal), ...] по возрастанию обеих координат.
    Пустой вход → []. Детерминированно.

    Связки (одинаковые p_raw) ПРЕДАГРЕГИРУЮТСЯ до PAV — иначе порядок исходов внутри
    связки рождает ложные ступени (два узла на одном x с p_cal 0 и 1). Боевые данные
    полны связок (П-1 давала повторяющиеся сырые вероятности)."""
    ties = {}
    for i, (p, y) in enumerate(zip(probs, outcomes)):
        w = float(weights.get(i, 1.0)) if isinstance(weights, dict) else 1.0
        acc = ties.setdefault(float(p), [0.0, 0.0])
        acc[0] += float(y) * w
        acc[1] += w
    if not ties:
        return []
    # блоки PAV: (сумма y*w, сумма w, min_p, max_p)
    blocks = []
    for p in sorted(ties):
        s, w = ties[p]
        blocks.append([s, w, p, p])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            s2, w2, lo2, hi2 = blocks.pop()
            blocks[-1][0] += s2
            blocks[-1][1] += w2
            blocks[-1][3] = hi2
    return [(round((b[2] + b[3]) / 2.0, 6), round(b[0] / b[1], 6)) for b in blocks]


def pav_apply(mapping, p):
    """Применить изотоническую карту к сырой вероятности (ступеньки с линейной интерполяцией
    между узлами; за краями — клип к крайним калиброванным значениям, без экстраполяции)."""
    if not mapping:
        return float(p)
    xs = [m[0] for m in mapping]
    p = float(p)
    if p <= xs[0]:
        return mapping[0][1]
    if p >= xs[-1]:
        return mapping[-1][1]
    i = bisect.bisect_right(xs, p)
    x0, y0 = mapping[i - 1]
    x1, y1 = mapping[i]
    if x1 == x0:
        return y1
    t = (p - x0) / (x1 - x0)
    return round(y0 + t * (y1 - y0), 6)


def brier(probs, outcomes):
    pairs = list(zip(probs, outcomes))
    if not pairs:
        return None
    return round(sum((float(p) - float(y)) ** 2 for p, y in pairs) / len(pairs), 6)


def _day_of(rec):
    return str(rec.get("sealed_at") or "")[:10]


def fit_walk_forward(rows, *, train_frac=0.7, n_boot=500, seed=20261101):
    """rows: [{'p_raw','outcome','sealed_at'}, ...] (только разрешённые исходы 0/1).

    Возвращает отчёт: mapping (фит на train по времени), метрики OOS (brier raw/cal/const),
    спред калиброванных значений, кластерный бутстрэп-CI спреда и Brier-преимущества
    (ресэмпл ДНЕЙ), честные размеры выборок."""
    rows = [r for r in rows if r.get("p_raw") is not None and r.get("outcome") in (0, 1)]
    rows.sort(key=lambda r: str(r.get("sealed_at") or ""))
    n = len(rows)
    if n < 100:
        return {"ОТКАЗ": f"мало данных для честного фита: {n} < 100"}
    cut = int(n * train_frac)
    train, test = rows[:cut], rows[cut:]
    mapping = pav_fit([r["p_raw"] for r in train], [r["outcome"] for r in train])
    p_cal = [pav_apply(mapping, r["p_raw"]) for r in test]
    p_raw = [r["p_raw"] for r in test]
    y = [r["outcome"] for r in test]
    base = sum(r["outcome"] for r in train) / len(train)
    cal_vals = sorted(set(round(v, 4) for v in p_cal))
    spread = round((cal_vals[-1] - cal_vals[0]) * 100, 1) if cal_vals else 0.0
    out = {
        "n_всего": n, "n_train": len(train), "n_test": len(test),
        "дней_в_тесте": len({_day_of(r) for r in test}),
        "узлов_изотоники": len(mapping), "mapping": mapping,
        "базовая_частота_train": round(base, 4),
        "OOS": {"brier_raw": brier(p_raw, y), "brier_калибр": brier(p_cal, y),
                "brier_константы_базы": brier([base] * len(y), y)},
        "спред_калиброванных_пп": spread,
    }
    # кластерный бутстрэп по дням теста: CI спреда и преимущества Brier над константой
    days = {}
    for r, pc in zip(test, p_cal):
        days.setdefault(_day_of(r), []).append((pc, r["outcome"]))
    day_keys = sorted(days)
    rng = random.Random(seed)
    adv, spreads = [], []
    for _ in range(n_boot):
        sample = [d for d in (rng.choice(day_keys) for _ in day_keys)]
        pc, yy = [], []
        for d in sample:
            for a, b in days[d]:
                pc.append(a)
                yy.append(b)
        if not pc:
            continue
        b_cal, b_const = brier(pc, yy), brier([base] * len(yy), yy)
        adv.append(b_const - b_cal)
        vals = sorted(pc)
        spreads.append((vals[-1] - vals[0]) * 100)
    if adv:
        adv.sort()
        spreads.sort()
        lo, hi = int(0.025 * len(adv)), max(0, int(0.975 * len(adv)) - 1)
        out["бутстрэп_по_дням"] = {
            "n_boot": n_boot,
            "преимущество_brier_над_константой_CI95": [round(adv[lo], 6), round(adv[hi], 6)],
            "спред_пп_CI95": [round(spreads[lo], 1), round(spreads[hi], 1)],
            "преимущество_значимо": bool(adv[lo] > 0),
        }
    return out


def report_to_yaml_dict(report, *, track, source_note):
    """Отчёт → словарь для knowledge/prob_recalib_proposal.yaml (применение — по подписи/гейту)."""
    return {
        "track": track, "метод": "изотоническая (PAV), walk-forward, кластерный бутстрэп по дням",
        "источник": source_note,
        "mapping": [{"p_raw": a, "p_cal": b} for a, b in report.get("mapping", [])],
        "метрики": {k: report[k] for k in ("n_всего", "n_train", "n_test", "дней_в_тесте",
                                           "базовая_частота_train", "OOS",
                                           "спред_калиброванных_пп") if k in report},
        "бутстрэп_по_дням": report.get("бутстрэп_по_дням"),
        "статус": "ПРЕДЛОЖЕНИЕ (фит детерминирован; подключение к боевому seal — stage-review)",
    }
