# -*- coding: utf-8 -*-
"""orchestrator/synthesis.py — блок F §4: скоринг §7, риск-агент, портфель, синтез отчёта §8.

Этапы 4 и 6 воронки §6. Скоринг §7 и портфель — ДЕТЕРМИНИРОВАННЫЙ код (mathlib.scoring,
mathlib.portfolio), риск-агент и синтезатор — LLM (блок F). Иерархия §5: «нет» риска и
портфеля перебивает энтузиазм поля — это применяется в funnel после синтеза.

Per-критериальные оценки скоринга §7 выводятся из ПОЛЯ СУЖДЕНИЙ (вердикты D/C/G агентов) и
издержек costs.yaml — честно, с консервативной заменой и пометкой пробела (П8), а НЕ выдумкой.
"""
import math
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COSTS_PATH = ROOT / "config" / "costs.yaml"

_DEFAULT_HORIZON_DAYS = 10.0   # консервативный дефолт (≈2 недели), если срок идеи не распознан

# Маркеры единиц срока → торговых дней. «кв»/«квартал» только явные, чтобы не ловить мусор.
_UNIT_DAYS = [
    (("квартал", "кварт", "кв."), 63.0),
    (("месяц", "мес"), 21.0),
    (("недел", "нед."), 5.0),
    (("суток", "сут", "день", "дней", "дня", "дн"), 1.0),
]


def _parse_horizon_text(text):
    """Самый ДЛИННЫЙ распознанный срок (число+единица) из текста, в торговых днях. None — не распознан.
    Берём максимум консервативно: инвалидация по более длинному горизонту шире (§4 — не выбивать шумом)."""
    t = str(text or "").lower()
    best = None
    for markers, mult in _UNIT_DAYS:
        for mk in markers:
            idx = t.find(mk)
            while idx != -1:
                nums = re.findall(r"\d+(?:[.,]\d+)?", t[max(0, idx - 14):idx])
                if nums:
                    val = float(nums[-1].replace(",", ".")) * mult
                    best = val if best is None else max(best, val)
                idx = t.find(mk, idx + 1)
        if best is not None:
            return best     # единицы упорядочены крупное→мелкое: первое совпадение точнее
    return None


def _idea_horizon_days(candidate):
    """Горизонт удержания идеи в торговых днях. Нужен риск-модулю: масштаб ожидаемого хода,
    издержки шорта (заёмка × дни) и инвалидация ЗАВИСЯТ от срока (§4, §7). None — неизвестен.
    Источники по убыванию специфичности: явный 'срок' в разрешимости §9 → числовой 'горизонт' →
    категориальный 'горизонт' ('дни'/'недели')."""
    raw = candidate.get("горизонт")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    spec = _parse_horizon_text(candidate.get("разрешимость"))
    if spec is not None:
        return spec
    spec = _parse_horizon_text(raw)
    if spec is not None:
        return spec
    cat = str(raw or "").lower()
    if "недел" in cat:
        return 10.0          # ≈2 недели — сохраняет прежний дефолт
    if "дн" in cat or "день" in cat:
        return 5.0
    return None

import sys
sys.path.insert(0, str(ROOT))
from mathlib import scoring as SC      # noqa: E402
from mathlib import portfolio as PF    # noqa: E402
from orchestrator import agents as A   # noqa: E402


def load_costs(path=COSTS_PATH):
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("instruments", {})


def _rec(records_by_id, aid):
    r = records_by_id.get(aid)
    return r["judgment"] if (r and r.get("ok")) else None


# ── Вывод per-критериальных оценок §7 из поля суждений (этап 4) ──────────────────
def derive_criteria(candidate, records_by_id, ctx, costs, *, judge_prob=None):
    """6 критериев §7 в [0,1] + протокол их происхождения (что измерено, что заменено, П8)."""
    asset = candidate.get("актив")
    direction = str(candidate.get("направление", "")).strip().lower()
    notes, missing = [], []

    # 1. Вероятность успеха — судья (этап 5) приоритетнее школьной оценки
    prob = judge_prob if judge_prob is not None else candidate.get("вероятность_школы")
    if prob is None:
        prob, src = 0.5, "нет данных → 0.5"
        missing.append("probability_success")
    else:
        src = "судья (этап 5)" if judge_prob is not None else "вероятность школы (этап 2)"
    notes.append(f"probability_success={round(prob,3)} ({src})")
    probability_success = float(prob)

    # 2. Асимметрия net — из издержек round_trip_bps + ATR-проксиед движений
    inst = (costs or {}).get(asset, {})
    rt = inst.get("round_trip_bps")
    ind = (ctx or {}).get("indicators", {}).get(asset, {})
    atr, last = ind.get("atr14"), ind.get("last_close")
    if rt is not None and atr and last:
        daily_bps = atr / last * 1e4  # дневной ход в бп (ATR)
        # ход и инвалидация масштабируются по ГОРИЗОНТУ тезиса (~√времени), а не по дневному ATR:
        # многоквартальная идея не может «гарантированно выбиваться» дневным стопом (баг §4, mc10).
        horizon_days = _idea_horizon_days(candidate) or _DEFAULT_HORIZON_DAYS
        move_bps = daily_bps * math.sqrt(horizon_days / _DEFAULT_HORIZON_DAYS)
        borrow = inst.get("short_borrow_fee_bps") if direction == "шорт" else None
        na = SC.net_asymmetry_score(probability_success, rt,
                                    win_move_bps=move_bps * 2, loss_move_bps=move_bps,
                                    short_borrow_bps=borrow, horizon_days=horizon_days)
        asymmetry_net = na["score"]
        notes.append(f"asymmetry_net={asymmetry_net} (EV={na['ev_bps']}бп, round_trip={rt}бп, "
                     f"горизонт≈{round(horizon_days)}д, ход×√(H/{int(_DEFAULT_HORIZON_DAYS)}))")
        if na["borrow_assumed_zero"] and direction == "шорт":
            missing.append("asymmetry_net(short_borrow)")
            notes.append(na["borrow_note"])
    else:
        asymmetry_net = 0.5
        missing.append("asymmetry_net")
        notes.append("asymmetry_net=0.5 (нет round_trip/ATR — консервативная замена, П8)")

    # 3. Неочевидность — c_non_obviousness (ШТРАФ→низко) + тайминг (ПОЗДНО/ЛОВУШКА→низко)
    # тайминг берём ПЕР-КАНДИДАТНЫЙ (этап 3), если есть; иначе тематический из поля
    non_obv = 0.6
    nz = _rec(records_by_id, "c_non_obviousness")
    if nz and str(nz.get("вердикт")).upper() == "ШТРАФ":
        non_obv = 0.25
    tv = str(candidate.get("_тайминг") or "").upper()
    if not tv:
        tm = _rec(records_by_id, "d_timeliness")
        tv = str((tm or {}).get("вердикт", "")).upper()
    if tv in ("ПОЗДНО", "ЛОВУШКА"):
        non_obv = min(non_obv, 0.2)
    elif tv == "ВОВРЕМЯ":
        non_obv = max(non_obv, 0.65)
    if not nz and not tv:
        missing.append("non_obviousness")
    notes.append(f"non_obviousness={non_obv} (неочевидность={nz and nz.get('вердикт')}, тайминг={tv or '—'})")

    # 4. Надёжность данных — валидатор/credibility (OK→высоко, ВОЗВРАТ→низко)
    data_rel, hits = 0.6, []
    for aid in ("g_validator", "g_credibility", "e_data_reviewer"):
        v = _rec(records_by_id, aid)
        if v:
            hits.append(str(v.get("вердикт")).upper())
    if hits:
        if any(h in ("ВОЗВРАТ", "КАРАНТИН") for h in hits):
            data_rel = 0.3
        elif all(h == "OK" for h in hits):
            data_rel = 0.8
    else:
        missing.append("data_reliability")
    notes.append(f"data_reliability={data_rel} (контроль={hits or '—'})")

    # 5. Контролируемость риска — манип-балл (низкий→высоко) + ликвидность (ADV) + шорт штраф
    # манип-балл берём ПЕР-КАНДИДАТНЫЙ (этап 3), если есть
    risk_ctrl = 0.6
    mscore = candidate.get("_манип_балл")
    mp = _rec(records_by_id, "d_anti_manipulation") if mscore is None else {"балл": mscore}
    if mp and isinstance(mp.get("балл"), (int, float)):
        risk_ctrl = max(0.1, 1.0 - mp["балл"] / 10.0)
    adv_usd = inst.get("adv_usd_median")
    if adv_usd and adv_usd < 1e7:  # тонкий рынок → ниже контролируемость
        risk_ctrl = min(risk_ctrl, 0.5)
    if direction == "шорт":
        risk_ctrl *= 0.85  # short ≠ зеркальный long (§4): неогр. убыток/сквиз
    risk_ctrl = round(risk_ctrl, 4)
    if not mp:
        missing.append("risk_controllability")
    notes.append(f"risk_controllability={risk_ctrl} (манип={mp and mp.get('балл')}, ADV={adv_usd})")

    # 6. Близость к компетенции — c_context_filter (OK→высоко, ШТРАФ→низко)
    comp = 0.7  # ядро универсума §13 (сырьё) внутри круга компетенции
    cf = _rec(records_by_id, "c_context_filter")
    if cf and str(cf.get("вердикт")).upper() == "ШТРАФ":
        comp = 0.3
    if not cf:
        missing.append("competence_proximity")
    notes.append(f"competence_proximity={comp} (фильтр={cf and cf.get('вердикт')})")

    return {
        "values": {
            "probability_success": round(probability_success, 4),
            "asymmetry_net": round(asymmetry_net, 4),
            "non_obviousness": round(non_obv, 4),
            "data_reliability": round(data_rel, 4),
            "risk_controllability": risk_ctrl,
            "competence_proximity": round(comp, 4),
        },
        "происхождение": notes,
        "пробелы_П8": missing,
    }


def score_candidate(candidate, records_by_id, ctx, costs, *, judge_prob=None, min_score=0.0):
    """Скоринг §7 одного кандидата: критерии → взвешенный балл (mathlib.scoring)."""
    crit = derive_criteria(candidate, records_by_id, ctx, costs, judge_prob=judge_prob)
    result = SC.score_idea(crit["values"], min_score=min_score)
    result["происхождение_критериев"] = crit["происхождение"]
    result["пробелы_П8"] = crit["пробелы_П8"]
    return result


# ── Риск-агент (LLM, этап 6) ─────────────────────────────────────────────────────
def run_risk(candidate, ctx, client, costs):
    """Вызов риск-агента §4 блок F по идее. Возвращает запись агента + флаг short_borrow."""
    import json
    asset, direction = candidate.get("актив"), str(candidate.get("направление", "")).lower()
    inst = (costs or {}).get(asset, {})
    payload = {
        "идея": {"актив": asset, "направление": candidate.get("направление"),
                 "тезис": candidate.get("тезис"), "разрешимость": candidate.get("разрешимость")},
        "издержки": {"round_trip_bps": inst.get("round_trip_bps"),
                     "short_borrow_fee_bps": inst.get("short_borrow_fee_bps"),
                     "short_borrow_note": ("для ШОРТА: short_borrow_fee_bps=null — нет данных (П8); "
                                           "истинные издержки шорта ЗАНИЖЕНЫ, закладывай консервативно")},
        "котировка": (ctx or {}).get("quotes", {}).get(asset, {}).get("last"),
        "индикаторы": (ctx or {}).get("indicators", {}).get(asset),
        "вероятность_судьи": candidate.get("вероятность_судьи"),
    }
    user = ("Оценка риска по идее (§4 блок F, §8 п.4/8/12). Только поданные данные (П8).\n\n"
            "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1, default=str) +
            "\n```\n\nВерни РОВНО один объект JSON по контракту.")
    rec = A.call_agent("f_risk", ctx, client, user_prompt=user)
    rec["short_borrow_no_data"] = (direction == "шорт" and inst.get("short_borrow_fee_bps") is None)
    return rec


# ── Синтез отчёта §8 (LLM, этап 6) ────────────────────────────────────────────────
def synthesize_report(idea_bundle, ctx, client):
    """Синтезатор §8: 13 полей из готовых компонентов (гипотеза/судья/скоринг/риск/портфель)."""
    import json
    user = ("Сборка отчёта по идее (13 обязательных полей §8) из ПОДАННЫХ компонентов. Не "
            "выдумывай числа — бери из компонентов; чего нет — «нет данных» (П8).\n\n"
            "```json\n" + json.dumps(idea_bundle, ensure_ascii=False, indent=1, default=str) +
            "\n```\n\nВерни РОВНО один объект JSON по контракту (ключ 'поля' с 13 полями).")
    return A.call_agent("f_synthesizer", ctx, client, user_prompt=user)
