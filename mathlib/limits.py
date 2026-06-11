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
