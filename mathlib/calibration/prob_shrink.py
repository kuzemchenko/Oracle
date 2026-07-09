# -*- coding: utf-8 -*-
"""mathlib/calibration/prob_shrink.py — сжатие вероятностей к базовой частоте по форвард-исходам.

Разбор 09.07: система заявляет среднюю уверенность 0.75–0.79 при попадании 40–45% —
систематическое самомнение. Лечение по §10 (петля качества, N≥30): линейное сжатие
    p' = p0 + λ·(p − p0),
где p0 — базовая частота исходов трека, λ∈[0,1] — доля доверия собственной уверенности.
λ подбирается минимизацией Brier на РАЗРЕШЁННЫХ форвард-исходах (П16: только форвард,
никакой истории до cutoff моделей). Честность: fit_lambda_walkforward подгоняет λ на
ранней части исходов и меряет улучшение на поздней (out-of-sample), чтобы предложение
не было подгонкой под самого себя.

ПРИМЕНЕНИЕ — только с подписью владельца (инвариант 5 CLAUDE.md: критерии/веса — с согласия).
Этот модуль только СЧИТАЕТ предложение (детерминированно, инвариант 6). LLM нет.
"""
import pathlib

from mathlib.brier import brier_score

ROOT = pathlib.Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "knowledge" / "prob_shrink.yaml"

GRID = [round(x * 0.05, 2) for x in range(0, 21)]     # λ ∈ {0.00, 0.05, …, 1.00}
MIN_N_FIT = 30                                         # §10: порог применимости поправки


def load_policy(path=None):
    """Подписанная политика сжатия (П-1 09.07) из knowledge/prob_shrink.yaml.
    Нет файла / битый / нет обязательных полей → None (сжатие НЕ применяется — fail-open
    сознательно: отсутствие политики = поправка не подписана, печатаем сырую вероятность)."""
    import yaml
    p = pathlib.Path(path) if path else POLICY_PATH
    if not p.exists():
        return None
    try:
        pol = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:                                  # noqa: BLE001 — битый YAML = нет политики
        return None
    if pol.get("lambda") is None or pol.get("p0") is None or not pol.get("applies_to"):
        return None
    return pol


def shrink(p, p0, lam):
    """p' = p0 + λ(p − p0), обрезка в [0.01, 0.99] (вероятность 0/1 не заявляем — П8)."""
    return min(0.99, max(0.01, p0 + lam * (p - p0)))


def fit_lambda(probs, outcomes, p0=None):
    """λ с минимальным Brier на данных (сетка GRID). Возвращает (λ, brier_при_λ, p0).
    p0 по умолчанию — фактическая базовая частота выборки."""
    if len(probs) < MIN_N_FIT:
        return None, None, None
    p0 = sum(outcomes) / len(outcomes) if p0 is None else p0
    best_lam, best_b = None, None
    for lam in GRID:
        b = brier_score([shrink(p, p0, lam) for p in probs], outcomes)
        if best_b is None or b < best_b:
            best_lam, best_b = lam, b
    return best_lam, round(best_b, 4), round(p0, 4)


def fit_lambda_walkforward(rows, split=0.6):
    """rows: [(prob, outcome, resolved_at_iso)] в любом порядке. Подгонка λ и p0 на ранних
    split-долях (по времени сверки), оценка на поздних (OOS). Возвращает протокол-предложение."""
    rows = sorted(rows, key=lambda r: str(r[2]))
    n_fit = int(len(rows) * split)
    fit, oos = rows[:n_fit], rows[n_fit:]
    if len(fit) < MIN_N_FIT or not oos:
        return {"применимо": False,
                "причина": f"мало исходов: fit={len(fit)} (< {MIN_N_FIT}) или oos={len(oos)}=0 (§10)"}
    lam, _, p0 = fit_lambda([r[0] for r in fit], [r[1] for r in fit])
    oos_p, oos_o = [r[0] for r in oos], [r[1] for r in oos]
    b_raw = brier_score(oos_p, oos_o)
    b_shr = brier_score([shrink(p, p0, lam) for p in oos_p], oos_o)
    # ориентир «монетки»: всегда P=0.5
    b_coin = brier_score([0.5] * len(oos_o), oos_o)
    return {"применимо": True, "lambda": lam, "p0_fit": p0,
            "n_fit": len(fit), "n_oos": len(oos),
            "brier_oos_без_сжатия": round(b_raw, 4),
            "brier_oos_со_сжатием": round(b_shr, 4),
            "brier_oos_монетка_0.5": round(b_coin, 4),
            "улучшение_oos": round(b_raw - b_shr, 4),
            "spec_ref": "§10 поправки по форвард-статистике; §25 предложение; П16 форвард-онли"}
