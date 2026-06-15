# -*- coding: utf-8 -*-
"""mathlib/calibration/sensitivity.py — walk-forward калибровка КАСКАДНЫХ ЧУВСТВИТЕЛЬНОСТЕЙ
(бет переноса) для движка mathlib/cascade.py (Этап 3 PLAN_cascade_first.md, §23.1 честная зона).

Что меряем: историческую бету доходностей узла к доходности источника (sensitivity) НА КАЖДОМ
walk-forward окне → проверяем УСТОЙЧИВОСТЬ. Пиним бету (median по фолдам) ТОЛЬКО если:
  • достаточно фолдов; • знак беты согласован по фолдам; • относительный разброс мал;
  • перенос установлен (Fisher-CI корреляции исключает 0) в большинстве фолдов.
Иначе — НЕ пиним: «калибруется только форвардом» (честно, как timing.spent_move_sigma в thresholds).

§23.1: код не помнит будущее — бета каждого фолда считается на train-срезе. П8: мало истории →
запись с pinned=None и причиной, без выдуманного числа.
"""
import statistics as stat

from mathlib import cascade as CAS
from mathlib.calibration import walkforward as WF

TRAIN = 504          # ~2 года торговых дней на train-окно
TEST = 252           # ~1 год на test (окно сдвигается на step)
STEP = 126           # шаг сдвига ~полгода
MIN_FOLDS = 3
REL_DISP_MAX = 0.35  # относительный разброс бет (IQR/|median|) выше → не пиним
ESTAB_FRAC_MIN = 0.6 # доля фолдов с установленным переносом для пина


def calibrate_pair_sensitivity(source_ret, node_ret, lag=0,
                               train=TRAIN, test=TEST, step=STEP, min_obs=CAS.MIN_OBS):
    """Walk-forward бета node←source на лаге переноса. Возвращает запись с pinned/provenance."""
    s, nd = CAS._align_lag(source_ret, node_ret, lag)
    n = min(len(s), len(nd))
    full = CAS.node_sensitivity(s, nd, lag=0, min_obs=min_obs)  # уже выровнены — лаг учтён
    if n < train + test:
        return {"lag": int(lag), "n_obs": int(n), "pinned": None, "beta_pinned": None,
                "beta_fullsample": (full or {}).get("beta"),
                "provenance": f"нет данных (П8): история {n} < train+test ({train}+{test}) — "
                              "чувствительность калибруется только форвардом"}
    betas, established = [], 0
    folds = WF.walk_forward(n, train, test, step=step)
    for fd in folds:
        tr = fd.train
        fit = CAS.node_sensitivity(s[tr], nd[tr], lag=0, min_obs=min_obs)
        if fit is None:
            continue
        betas.append(fit["beta"])
        established += int(fit["перенос_установлен"])
    if len(betas) < MIN_FOLDS:
        return {"lag": int(lag), "n_obs": int(n), "n_folds": len(betas), "pinned": None,
                "beta_pinned": None, "beta_fullsample": (full or {}).get("beta"),
                "provenance": f"нет данных (П8): валидных фолдов {len(betas)} < {MIN_FOLDS}"}
    med = stat.median(betas)
    sign_consistent = all(b > 0 for b in betas) or all(b < 0 for b in betas)
    iqr = (stat.quantiles(betas, n=4)[2] - stat.quantiles(betas, n=4)[0]) if len(betas) >= 4 else \
        (max(betas) - min(betas))
    rel_disp = abs(iqr / med) if med else float("inf")
    estab_frac = established / len(betas)
    pinned = bool(sign_consistent and rel_disp <= REL_DISP_MAX and estab_frac >= ESTAB_FRAC_MIN)
    rec = {
        "lag": int(lag), "n_obs": int(n), "n_folds": len(betas),
        "beta_pinned": round(med, 6) if pinned else None,
        "pinned": pinned,
        "beta_fullsample": (full or {}).get("beta"),
        "beta_ci_folds": [round(min(betas), 6), round(max(betas), 6)],
        "fold_betas": [round(b, 4) for b in betas],
        "sign_consistent": sign_consistent,
        "rel_dispersion": round(rel_disp, 4),
        "established_frac": round(estab_frac, 3),
        "r2_fullsample": (full or {}).get("r2"),
    }
    if pinned:
        rec["provenance"] = (f"ПИН: знак согласован, разброс {rec['rel_dispersion']} ≤ {REL_DISP_MAX}, "
                             f"перенос установлен в {estab_frac:.0%} фолдов ({len(betas)})")
    else:
        why = []
        if not sign_consistent: why.append("знак беты непостоянен по фолдам")
        if rel_disp > REL_DISP_MAX: why.append(f"разброс {rec['rel_dispersion']} > {REL_DISP_MAX}")
        if estab_frac < ESTAB_FRAC_MIN: why.append(f"перенос установлен лишь в {estab_frac:.0%} фолдов")
        rec["provenance"] = "НЕ ПИНИТСЯ (форвард-онли): " + "; ".join(why)
    return rec
