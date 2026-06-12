# -*- coding: utf-8 -*-
"""mathlib/portfolio.py — портфельный менеджер §4/§7 (MASTER_SPEC, инвариант 6 CLAUDE.md).

§4 «Портфельный менеджер»:
  • Дробный Келли со СТЯГИВАНИЕМ вероятности к 50% пропорционально недоказанности калибровки;
    включается ТОЛЬКО после gate калибровки — до того ФИКС 0.5% капитала/идея (mathlib.kelly).
  • Карта корреляций активных идей по МАКРО-ДРАЙВЕРАМ: лонг меди + лонг чилийского песо +
    шорт авиалиний = ОДНА ставка, не три. Группируем идеи по драйверу и считаем нетто-экспозицию.
  • Общий лимит просадки → стоп всей системы до разбора (mathlib.limits / config/limits.yaml).

Размер позиции считает КОД, не LLM (§21). Здесь — детерминированная сборка портфеля поверх
mathlib.kelly.position_size: размер каждой идеи + кластеризация по драйверам + лимит-ворота.
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
UNIVERSE_PATH = ROOT / "config" / "universe.yaml"

from mathlib import kelly  # noqa: E402
from mathlib import limits as lim  # noqa: E402

# Карта тикер → МАКРО-ДРАЙВЕР (§4: коррелированные идеи = одна ставка). Драйвер определяет
# общий риск-фактор; идеи с одним драйвером и согласованным знаком экспозиции суммируются.
# Источник: состав универсума (config/universe.yaml). Помечено как инженерная разметка —
# меняется с составом универсума (решение пользователя).
MACRO_DRIVER = {
    "BNO.US": "oil",            # Brent-прокси
    "USO.US": "oil",            # WTI-прокси
    "CPER.US": "copper",        # медь-прокси
    "COPX.US": "copper",        # медники (equity) — тот же драйвер «медь»
    "FCX.US": "copper",
    "SCCO.US": "copper",
    "DBC.US": "broad_commodity",  # широкий сырьевой индекс-прокси (пересекается с oil/copper по бете)
    "SPY.US": "equity_beta",      # широкий рынок акций
}
# Драйверы, между которыми есть известная положительная сырьевая бета (для предупреждения о
# скрытой концентрации; НЕ для тихого суммирования — это другой, более слабый уровень связи).
COMMODITY_FAMILY = {"oil", "copper", "broad_commodity"}


def macro_driver(symbol):
    """Макро-драйвер тикера; неизвестный тикер → 'unmapped' (П8: не выдумываем связь)."""
    return MACRO_DRIVER.get(symbol, "unmapped")


def _signed_driver(symbol, direction):
    """Подписанный драйвер: лонг oil и шорт oil — противоположные экспозиции одного фактора."""
    sign = "+" if str(direction).strip().lower() == "лонг" else "-"
    return f"{sign}{macro_driver(symbol)}"


def correlation_map(ideas):
    """Карта корреляций по макро-драйверам (§4). ideas: список {актив, направление, amount_usd}.

    Группирует по ПОДПИСАННОМУ драйверу: одинаковый знак+драйвер = одна ставка (нетто
    суммируется). Возвращает кластеры с суммарной экспозицией и предупреждения о скрытой
    концентрации внутри сырьевого семейства.
    """
    clusters = {}
    for it in ideas:
        key = _signed_driver(it["актив"], it.get("направление", "лонг"))
        c = clusters.setdefault(key, {"подписанный_драйвер": key, "идеи": [], "суммарный_размер_usd": 0.0})
        c["идеи"].append(it["актив"])
        c["суммарный_размер_usd"] = round(c["суммарный_размер_usd"] + float(it.get("amount_usd", 0.0)), 2)

    # предупреждение: несколько РАЗНЫХ сырьевых драйверов в одну сторону → скрытая сырьевая ставка
    commodity_long = [k for k in clusters if k.startswith("+") and k[1:] in COMMODITY_FAMILY]
    warnings = []
    if len(commodity_long) > 1:
        warnings.append({
            "тип": "скрытая_концентрация",
            "деталь": f"несколько лонгов сырьевых драйверов {commodity_long} имеют общую сырьевую бету — "
                      "относиться как к усиленной ОДНОЙ сырьевой ставке (§4)",
        })
    # одна и та же ставка, размазанная по нескольким тикерам
    for key, c in clusters.items():
        if len(c["идеи"]) > 1:
            warnings.append({
                "тип": "одна_ставка_много_тикеров",
                "деталь": f"{c['идеи']} делят драйвер '{key}' — это ОДНА ставка, не {len(c['идеи'])} (§4)",
            })
    return {"кластеры": list(clusters.values()), "предупреждения": warnings,
            "n_независимых_ставок": len(clusters)}


def build_portfolio(ideas, *, capital, gate_passed=False, calibration_proven=0.0,
                    kelly_multiplier=0.5, limits=None):
    """Соберать портфель из идей (§4 портфельный менеджер).

    Каждая идея: {актив, направление, вероятность, b (net-оддсы)}. Размер — mathlib.kelly:
    до gate калибровки (gate_passed=False) ФИКС 0.5% капитала/идея; после — дробный Келли со
    стягиванием. Затем карта корреляций по драйверам и программная проверка лимитов §11.

    Возвращает dict: позиции (с размером и драйвером), карта_корреляций, проверка_лимитов.
    """
    limits = limits or lim.load_limits()
    micro_pct = limits["risk"]["per_idea_microsize_pct"]
    positions = []
    for it in ideas:
        p = it.get("вероятность")
        b = it.get("b", 1.0)
        sizing = kelly.position_size(
            p if p is not None else 0.5, b, capital,
            calibration_proven=calibration_proven, kelly_multiplier=kelly_multiplier,
            gate_passed=gate_passed, microsize_pct=micro_pct)
        positions.append({
            "актив": it["актив"], "направление": it.get("направление"),
            "вероятность": p, "b_net_odds": b,
            "макро_драйвер": macro_driver(it["актив"]),
            "amount_usd": round(sizing["amount_usd"], 2),
            "sizing_method": sizing["method"], "gate_passed": gate_passed,
        })
    cmap = correlation_map(positions)

    # программные ворота §11: риск на идею + суммарный риск месяца (потолки limits.yaml)
    total = round(sum(p["amount_usd"] for p in positions), 2)
    per_idea_checks = [{"актив": p["актив"], **lim.check_idea_risk(p["amount_usd"], limits=limits)}
                       for p in positions]
    monthly = lim.check_monthly_risk(0.0, total, limits=limits)
    return {
        "позиции": positions,
        "суммарный_риск_usd": total,
        "карта_корреляций": cmap,
        "проверка_лимитов": {"на_идею": per_idea_checks, "месячный": monthly},
        "режим_размера": ("фикс 0.5% капитала/идея (до gate калибровки, §11)" if not gate_passed
                          else "дробный Келли со стягиванием (gate пройден)"),
        "calibration_proven": calibration_proven,
    }
