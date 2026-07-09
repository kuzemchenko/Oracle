# -*- coding: utf-8 -*-
"""ops/refit_prob_shrink.py — ежемесячная пере-подгонка политики сжатия вероятностей (П-1, §10).

Подпись владельца 09.07.2026 (П-1) авторизует ИМЕННО этот цикл: «λ и p0 пере-подгоняются
ЕЖЕМЕСЯЧНО петлёй §25 walk-forward'ом (N≥30, §10)». Поэтому --apply здесь легитимен по
подписи (в отличие от promote_edges, где авто-apply удержан долгом B4(а)).

Данные: сверенные исходы боевых треков (money+provisional) × СЫРАЯ уверенность прогноза
(probability_raw, если печать шла со сжатием; иначе probability). Walk-forward: подгонка
на ранних 60% по времени сверки, контроль на поздних 40%. Пишет knowledge/prob_shrink.yaml
(--apply) и заметку владельцу в бота. Детерминированный код, LLM нет (инвариант #6).
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import sealing as SEAL                      # noqa: E402
from mathlib.calibration import prob_shrink as PS        # noqa: E402
from orchestrator import resolve as RES                  # noqa: E402

TRACKS = ("money", "provisional")
APPLIES_TO = ["cascade_money", "cascade_provisional"]


def collect_rows(predictions_path=None, outcomes_path=None):
    """(p_raw, outcome, resolved_at) боевых треков. p_raw — уверенность ДО сжатия (П-1)."""
    praw = {}
    for p in SEAL.read_predictions(predictions_path):
        if p.get("hash"):
            praw[p["hash"]] = (p.get("probability_raw")
                               if p.get("probability_raw") is not None else p.get("probability"))
    rows = []
    for o in RES.read_outcomes(outcomes_path):
        if RES.track_for_kind(o.get("kind")) not in TRACKS:
            continue
        if o.get("outcome") not in (0, 1):
            continue
        p = praw.get(o.get("hash"))
        if p is None:
            continue
        rows.append((float(p), int(o["outcome"]), str(o.get("resolved_at") or "")))
    return rows


def render_yaml(fit, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (
        "# knowledge/prob_shrink.yaml — политика сжатия вероятностей к базовой частоте (§10).\n"
        "# СГЕНЕРИРОВАНО ops/refit_prob_shrink.py (ежемесячная пере-подгонка по подписи П-1\n"
        "# 09.07.2026, journal/proposals/proposals_20260709_calibration_court.md). Правки руками\n"
        "# будут перезаписаны. Сырая уверенность хранится в probability_raw печатей.\n"
        f"applies_to: [{', '.join(APPLIES_TO)}]\n"
        f"lambda: {fit['lambda']}\n"
        f"p0: {fit['p0_fit']}\n"
        f"fitted_at: \"{now.strftime('%Y-%m-%d')}\"\n"
        f"fit: {{n_fit: {fit['n_fit']}, n_oos: {fit['n_oos']}, "
        f"brier_oos_raw: {fit['brier_oos_без_сжатия']}, "
        f"brier_oos_shrunk: {fit['brier_oos_со_сжатием']}, split: 0.6}}\n"
        "signed_by: \"владелец, 09.07.2026 (П-1: ежемесячная пере-подгонка авторизована)\"\n"
        "spec_ref: \"§10; П-1 09.07; mathlib/calibration/prob_shrink.py\"\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Пере-подгонка λ/p0 сжатия вероятностей (П-1, §10)")
    ap.add_argument("--apply", action="store_true",
                    help="записать knowledge/prob_shrink.yaml (иначе DRY: только показать)")
    a = ap.parse_args(argv)
    rows = collect_rows()
    fit = PS.fit_lambda_walkforward(rows)
    print(json.dumps(fit, ensure_ascii=False, indent=1))
    if not fit.get("применимо"):
        print("[refit_prob_shrink] поправка не применима — политика НЕ изменена (§10)")
        return 0
    if a.apply:
        PS.POLICY_PATH.write_text(render_yaml(fit), encoding="utf-8")
        print(f"[refit_prob_shrink] применено → {PS.POLICY_PATH} (λ={fit['lambda']}, p0={fit['p0_fit']})")
        try:
            sys.path.insert(0, str(ROOT / "ops"))
            import auto_review as AR
            AR._notice(
                f"🔁 Ежемесячная пере-подгонка шкалы уверенности (П-1, §10): λ={fit['lambda']}, "
                f"p0={fit['p0_fit']} (N={fit['n_fit']}+{fit['n_oos']}). Brier на отложенной части: "
                f"{fit['brier_oos_без_сжатия']} → {fit['brier_oos_со_сжатием']}. "
                f"λ>0 будет означать: уверенность системы начала нести сигнал.")
        except Exception as e:                            # noqa: BLE001 — заметка не роняет пере-подгонку
            print(f"[refit_prob_shrink] заметка в бота не записана: {e}")
    else:
        print("[refit_prob_shrink] DRY: без --apply политика не тронута")
    return 0


if __name__ == "__main__":
    sys.exit(main())
