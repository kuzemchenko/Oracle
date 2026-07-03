# -*- coding: utf-8 -*-
"""mathlib/limits.py — программная проверка риск-лимитов и бюджетов (MASTER_SPEC §11, §12, инвариант 5 CLAUDE.md).

ЛИМИТЫ ЗАШИТЫ В КОДЕ (config/limits.yaml). Оркестратор проверяет их ПРОГРАММНО перед каждым
прогоном/идеей. Система ОТКАЗЫВАЕТСЯ обсуждать превышение в моменте (§12): функции возвращают
allowed=False с причиной. Поднять потолок может ТОЛЬКО пользователь правкой limits.yaml (П12),
это НЕ предмет уговоров агентов. Денежные ворота §11 (gates) — не редактируются.
"""
import pathlib
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIMITS_PATH = ROOT / "config" / "limits.yaml"


def load_limits(path=None):
    path = pathlib.Path(path) if path is not None else LIMITS_PATH
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def gates(limits=None):
    """Денежные ворота §11 из config (НЕ редактируются кодом). Единый источник вместо хардкода 270."""
    return (limits or load_limits()).get("gates", {}) or {}


def paper_to_money_gate(limits=None, default=270):
    """Порог Gate Б→Д (разрешённых прогнозов). F2#22: единый источник, раньше 270 был зашит в resolve/calibrate."""
    return int(gates(limits).get("paper_to_money_predictions", default))


def check_kill_criteria(*, calibration_band_pp=None, n_money_resolved=0,
                        money_brier=None, money_base_rate=None, limits=None):
    """ДЕТЕРМИНИРОВАННАЯ проверка KILL-критериев §11 (Инв#6: код, не LLM). F2#22 (§1.11): пороги
    KILL были в limits.yaml без потребителя — KILL проверялся только LLM-скиллом.

    §11 (MASTER_SPEC.md:195) — KILL применяется ТОЛЬКО «после 270 разрешённых прогнозов»
    (=kill_no_edge_after_predictions). Это порог применимости ОБЕИХ веток: он защищает и от
    sunk cost, и от ложного KILL на недостатке данных (П8: нехватка данных ≠ KILL). ДО порога
    критерий НЕ ПРИМЕНИМ — возвращаем kill=False независимо от band/brier.

    После порога KILL объявляется, если ДОКАЗАН на данных:
      • калибровка хуже kill_calibration_band_pp (band ИЗМЕРИМА, см. brier.MIN_BIN_N) — КАНОН §11;
        band=None (нет корзин с N) → «не измерима» → НЕ KILL (П8).
      • edge: канон §11 = нет ЗНАЧИМОГО превышения над БЕНЧМАРКОМ (§30: 0.6SPY+0.4DBC).
        ВНИМАНИЕ: бенчмарк-контур в resolve пока НЕ подключён. Поэтому edge-ветка выдаёт лишь
        ДИАГНОСТИКУ (Brier-скилл над климатологией) и НЕ ставит kill=True — иначе это подмена
        неизменяемого §11 прокси-метрикой (Инв#4: KILL не редактируется). Каноничная edge-ветка
        (сравнение доходности стратегии с бенчмарком + тест значимости §10) — отдельный долг,
        требует бенчмарк-ряда и подписи владельца (слой FГ).
    Возвращает {kill, reasons, checks}."""
    g = gates(limits)
    reasons, checks = [], {}

    # Порог применимости §11 = «270 разрешённых прогнозов». Берём из kill_no_edge_after_predictions,
    # но с ЯВНЫМ дефолтом (paper_to_money_predictions → 270): иначе удаление одного ключа config
    # МОЛЧА глушило бы и калибровочную ветку (fail-open на неизменяемом §11 — Инв#4).
    kn = g.get("kill_no_edge_after_predictions", g.get("paper_to_money_predictions", 270))
    threshold = int(kn) if kn is not None else 270
    applicable = n_money_resolved >= threshold
    checks["порог_применимости"] = {"N": n_money_resolved, "порог": threshold,
                                    "применимо": bool(applicable)}
    if not applicable:
        # §11/П8: до порога разрешённых прогнозов KILL не применяется НИ по одной ветке.
        checks["статус"] = f"до порога {threshold} разрешённых прогнозов — KILL не применим (§11)"
        return {"kill": False, "reasons": reasons, "checks": checks}

    # --- Калибровочная ветка: КАНОН §11, детерминирована ---
    # Порог ±15 п.п. с дефолтом (спека §11) — страховка от fail-open, если ключ вычищен из config:
    # молча НЕ отключаем неизменяемую защиту (Инв#4), симметрично дефолту порога 270 выше.
    kb = g.get("kill_calibration_band_pp", 15)
    if calibration_band_pp is not None and kb is not None:
        bad = float(calibration_band_pp) > float(kb)
        checks["калибровка"] = {"band_pp": round(float(calibration_band_pp), 2),
                                "kill_порог_пп": kb, "нарушен": bad}
        if bad:
            reasons.append(f"калибровка {float(calibration_band_pp):.1f} п.п. > KILL-порога {kb} п.п. (§11)")
    else:
        checks["калибровка"] = {"статус": "не измерима (нет корзин с достаточным N)"}

    # --- Edge-ветка: ДИАГНОСТИКА (прокси), НЕ KILL: бенчмарк-контур §30 не подключён ---
    bss = None
    if money_brier is not None and money_base_rate is not None:
        clim = float(money_base_rate) * (1.0 - float(money_base_rate))
        bss = None if clim <= 0 else 1.0 - float(money_brier) / clim
    checks["edge_диагностика"] = {
        "bss_над_климатологией": (None if bss is None else round(bss, 4)),
        "влияет_на_kill": False,
        "примечание": "ПРОКСИ (скилл над климатологией), НЕ канонический §11 edge — "
                      "канон = значимое превышение над бенчмарком §30 (контур не подключён)",
    }

    return {"kill": bool(reasons), "reasons": reasons, "checks": checks}


def _deny(reason, **extra):
    return {"allowed": False, "reason": reason, **extra}


def _ok(reason="в пределах лимита", **extra):
    return {"allowed": True, "reason": reason, **extra}


def check_idea_risk(amount_usd, *, limits=None):
    """Риск на ОДНУ идею против потолка микроразмера (§30 п.3: 0.5% = $500/идея, этап Д)."""
    lim = limits or load_limits()
    cap = lim["risk"]["per_idea_microsize_usd"]
    if amount_usd < 0:
        return _deny("отрицательный риск недопустим", limit=cap)
    if amount_usd > cap:
        return _deny(f"риск на идею ${amount_usd} > потолка ${cap}/идея (§30 п.3)", limit=cap)
    return _ok(limit=cap)


def check_monthly_risk(spent_this_month_usd, new_amount_usd, *, limits=None):
    """Месячный риск-лимит пре-коммитмента (§30 п.3: 3% = $3000/мес)."""
    lim = limits or load_limits()
    cap = lim["risk"]["monthly_risk_cap_usd"]
    total = spent_this_month_usd + new_amount_usd
    if total > cap:
        return _deny(f"месячный риск ${total} > потолка ${cap} (§30 п.3); превышение не обсуждается (§12)",
                     limit=cap, would_be=total)
    return _ok(limit=cap, would_be=total)


def check_fast_basket(spent_fast_usd, new_amount_usd, *, limits=None):
    """Корзина «быстрых» идей (§12, §30 п.3: 10% = $300/мес, НЕПОПОЛНЯЕМАЯ)."""
    lim = limits or load_limits()
    cap = lim["risk"]["fast_basket_usd"]
    total = spent_fast_usd + new_amount_usd
    if total > cap:
        return _deny(f"корзина быстрых идей ${total} > ${cap}, непополняема в течение месяца (§12)",
                     limit=cap, would_be=total)
    return _ok(limit=cap, would_be=total)


def check_monthly_budget(spent_usd, *, limits=None):
    """Бюджет владения (§30 п.2). ≥alert_fraction → ВНИМАНИЕ (allowed=True); ≥потолок → прогоны СТОП."""
    lim = limits or load_limits()
    b = lim["budget"]
    cap = b["total_usd_month"]
    frac = spent_usd / cap if cap else 0.0
    if spent_usd >= cap:
        return _deny(f"бюджет ${spent_usd} ≥ потолка ${cap}/мес — прогоны СТОП (§30 п.2)",
                     limit=cap, fraction=frac, level="stop")
    if frac >= b.get("alert_fraction", 0.8):
        return _ok(f"ВНИМАНИЕ: израсходовано {frac:.0%} бюджета", limit=cap, fraction=frac, level="alert")
    return _ok(limit=cap, fraction=frac, level="ok")


def check_run_token_budget(mode, est_usd, *, limits=None):
    """Бюджет токенов на ШАГ/прогон (§24). Превышение = стоп и разбор."""
    lim = limits or load_limits()
    table = lim["per_run_token_budget_usd"]
    if mode not in table:
        return _deny(f"неизвестный режим прогона {mode!r}", known=list(table))
    cap = table[mode]
    if est_usd > cap:
        return _deny(f"оценка прогона ${est_usd} > бюджета режима '{mode}' ${cap} (§24) — стоп и разбор",
                     limit=cap)
    return _ok(limit=cap)
