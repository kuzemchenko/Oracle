# -*- coding: utf-8 -*-
"""mathlib/fdr.py — FDR-контроль Бенджамини–Хохберга (MASTER_SPEC §6, §23.1 п.6).

§6: при 200–500 проверках в день система «найдёт» закономерности в шуме гарантированно.
Аномалия считается СИГНАЛОМ только при q-value < q_value_max (config/thresholds.yaml, по умолч. 0.1)
по процедуре Бенджамини–Хохберга. Чистая математика, не LLM (§21).
"""
import numpy as np


def benjamini_hochberg(pvalues, q=0.10):
    """Процедура Бенджамини–Хохберга при уровне FDR q.

    Возвращает dict:
      rejected  — булев список (в ИСХОДНОМ порядке): H0 отклонена (= сигнал)
      qvalues   — BH-скорректированные q-значения (в исходном порядке), монотонные
      threshold — наибольший p-value, прошедший порог (0.0, если сигналов нет)
      n_signif  — число сигналов
    """
    p = np.asarray(pvalues, dtype=float)
    if p.ndim != 1:
        raise ValueError("pvalues должен быть 1-D")
    if p.size == 0:
        return {"rejected": [], "qvalues": [], "threshold": 0.0, "n_signif": 0}
    if np.any((p < 0) | (p > 1)):
        raise ValueError("p-значения должны быть в [0,1]")

    m = p.size
    order = np.argsort(p, kind="mergesort")          # стабильная сортировка
    p_sorted = p[order]
    ranks = np.arange(1, m + 1)

    # BH: наибольший k, где p_(k) <= (k/m)*q → отклонить все 1..k
    passed = p_sorted <= ranks / m * q
    if passed.any():
        kmax = int(np.max(np.nonzero(passed)[0]))    # 0-based индекс крупнейшего прошедшего
        threshold = float(p_sorted[kmax])
        reject_sorted = np.zeros(m, dtype=bool)
        reject_sorted[: kmax + 1] = True
    else:
        threshold = 0.0
        reject_sorted = np.zeros(m, dtype=bool)

    # Скорректированные q-значения: монотонный минимум «сверху вниз», обрезка к [0,1]
    q_raw = p_sorted * m / ranks
    q_adj = np.minimum.accumulate(q_raw[::-1])[::-1]
    q_adj = np.clip(q_adj, 0.0, 1.0)

    rejected = np.empty(m, dtype=bool)
    qvalues = np.empty(m, dtype=float)
    rejected[order] = reject_sorted
    qvalues[order] = q_adj
    return {
        "rejected": rejected.tolist(),
        "qvalues": qvalues.tolist(),
        "threshold": threshold,
        "n_signif": int(reject_sorted.sum()),
    }
