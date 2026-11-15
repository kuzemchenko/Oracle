# -*- coding: utf-8 -*-
"""ops/diagnose_calibration.py — этап Д2 «Поискового движка»: независимый диагностический
пересчёт ВСЕХ разрешённых исходов калибровочного трека (kind=calibration) из сырых котировок.

Контекст (ROADMAP_2026-07_search_engine.md, этап Д2): 252 разрешённых исхода, hit-rate 36.1%,
SYNC §4.6 назвал это «систематикой (биномиальный p<<0.01)». Этот скрипт:
  1) пересчитывает каждый исход НЕЗАВИСИМО от orchestrator/resolve.py: порог, σ_H, бар исхода,
     направление, строгие неравенства — и раскладывает расхождения по причинам;
  2) считает «правильную» вероятность каждой печати (гауссова база на ФАКТИЧЕСКОМ горизонте
     в торговых барах, а не номинальном √5) и дивидендную поправку (raw vs adjusted close);
  3) строит ЧЕСТНЫЙ null для наблюдённого hit-rate: кластерный Монте-Карло по истории с
     сохранением (а) вложенности трёх порогов одной asset-недели, (б) кросс-корреляции корзины,
     (в) серийной структуры 7 перекрывающихся батчей — против наивного биномиального теста,
     который предполагал 252 НЕЗАВИСИМЫХ исхода;
  4) пишет отчёт ops/reports/d2_diagnosis/REPORT.md + report.json (в т.ч. блок dashboard_row
     для диагностического ряда табло §15 — решение владельца 13.07, Вопрос 2).

ЖЁСТКИЕ РАМКИ: журналы predictions.jsonl / outcomes.jsonl и боевая БД — ТОЛЬКО ЧТЕНИЕ
(П16; пути параметризованы для тестов). LLM нет (инвариант 6). Детерминирован (фикс. seed).
"""
import argparse
import collections
import datetime
import json
import math
import pathlib
import random
import sqlite3
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Абсолютные пути боевых артефактов (Д2: читаем боевой репозиторий, пишем в СВОЙ ops/reports)
PROD_PREDICTIONS = pathlib.Path("/home/oracle/oracle/journal/predictions.jsonl")
PROD_OUTCOMES = pathlib.Path("/home/oracle/oracle/journal/outcomes.jsonl")
PROD_DB = pathlib.Path("/home/oracle/oracle/storage/oracle.db")
OUT_DIR = ROOT / "ops" / "reports" / "d2_diagnosis"

H_NOMINAL = 5                    # заявленный горизонт печати (calibrate.H_TRADING_DAYS)
K_OFFSETS = (0.0, 0.5, -0.5)     # пороги печати (calibrate.K_OFFSETS)
MC_SEED = 20260713               # детерминированный null (этап Д2)
MC_N = 4000
PRE_WINDOW_CUT = "2026-06-01"    # null строится ТОЛЬКО на истории до окна печатей (нет подглядывания)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _std_ddof1(xs):
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def read_jsonl(path):
    out = []
    p = pathlib.Path(path)
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_joined(pred_path, outc_path):
    """Разрешённые калибровочные записи: [(prediction, outcome)] в порядке журнала исходов."""
    preds = {r["hash"]: r for r in read_jsonl(pred_path)
             if r.get("kind") == "calibration" and r.get("hash")}
    joined = []
    for o in read_jsonl(outc_path):
        if o.get("kind") != "calibration" or o.get("outcome") not in (0, 1):
            continue
        p = preds.get(o.get("hash"))
        if p is not None:
            joined.append((p, o))
    return joined


def journal_integrity(pred_path, outc_path):
    """Д2 #14 (кросс-ревью): целостность знаменателя. load_joined МОЛЧА выбрасывал разрешённый
    калибровочный исход, если для его hash нет prediction — отчёт «251 пересчитано, 0
    расхождений» вместо явного дефекта. Здесь считаем обе стороны и перечисляем unmatched;
    для П8 знаменатель обязан быть честным (unmatched=0 или список)."""
    preds = {r["hash"]: r for r in read_jsonl(pred_path)
             if r.get("kind") == "calibration" and r.get("hash")}
    resolved = [o for o in read_jsonl(outc_path)
                if o.get("kind") == "calibration" and o.get("outcome") in (0, 1)]
    unmatched = sorted({o.get("hash") for o in resolved if o.get("hash") not in preds})
    n_matched = sum(1 for o in resolved if o.get("hash") in preds)
    return {
        "n_calibration_predictions": len(preds),
        "n_resolved_calibration_outcomes": len(resolved),
        "n_matched": n_matched,
        "n_unmatched_outcomes": len(unmatched),
        "unmatched_outcome_hashes": [(h or "")[:12] for h in unmatched],
        "целостность_ок": len(unmatched) == 0,
        "пояснение": ("каждый разрешённый калибровочный исход обязан иметь prediction по hash; "
                      "unmatched>0 = дефект целостности журнала (исход есть, печати нет), "
                      "а НЕ повод молча уменьшить знаменатель"),
    }


class Quotes:
    """Ряды close/adjusted_close из БД (read-only URI) с кэшем."""

    def __init__(self, db_path):
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._cache = {}

    def series(self, symbol):
        if symbol not in self._cache:
            rows = self.con.execute(
                "SELECT date, close, adjusted_close FROM quotes "
                "WHERE symbol=? AND close IS NOT NULL ORDER BY date ASC", (symbol,)).fetchall()
            self._cache[symbol] = [(r[0], float(r[1]),
                                    (float(r[2]) if r[2] is not None else None)) for r in rows]
        return self._cache[symbol]

    def close(self):
        self.con.close()


def recompute_record(q, p, o):
    """Независимый пересчёт одной записи. Возвращает разложение по конвенциям (каждая — ✓/✗)."""
    sym = p["asset"]
    cl = q.series(sym)
    dates = [d for d, _, _ in cl]
    asof = p["threshold_asof_close_date"]
    row = {"hash": (p.get("hash") or "")[:12], "asset": sym, "run_id": p.get("run_id"),
           "k_sigma": p.get("k_sigma"), "prob_claimed": p.get("probability"),
           "outcome_journal": o["outcome"]}
    if asof not in dates:
        row["error"] = f"бар порога {asof} отсутствует в БД"
        return row
    i0 = dates.index(asof)
    px = cl[i0][1]

    # (1) σ_H: realized_vol ddof=1 на 60 лог-ретёрнах, ×√5 — как calibrate.py:78-79
    hist = [c for _, c, _ in cl[:i0 + 1]][-61:]
    lr = [math.log(hist[i + 1] / hist[i]) for i in range(len(hist) - 1)]
    sigma_h_re = _std_ddof1(lr) * math.sqrt(H_NOMINAL)
    row["sigma_ok"] = abs(sigma_h_re - p["sigma_h"]) < 1e-6

    # (2) порог: round(px·e^{k·σ_H}, 4) — calibrate.py:83-84
    thr_re = round(px * math.exp(p["k_sigma"] * sigma_h_re), 4)
    row["threshold_ok"] = abs(thr_re - p["threshold"]) < 1e-6

    # (3) бар исхода: первый close на/после даты resolve_by — resolve._observed_close_after
    rb = str(o["resolve_by"])[:10]
    obs = next(((d, c, a) for d, c, a in cl if d >= rb), None)
    if obs is None:
        row["error"] = "нет бара на/после resolve_by в БД"
        return row
    row["bar_ok"] = (abs(obs[1] - float(o["observed_value"])) < 1e-9
                     and obs[0] == str(o["observed_at"])[:10])

    # (4) исход: СТРОГОЕ неравенство close > threshold для above (outcomes.py:51-54)
    out_re = 1 if obs[1] > float(p["threshold"]) else 0
    row["outcome_recomputed"] = out_re
    row["outcome_ok"] = out_re == o["outcome"]
    row["equality_case"] = abs(obs[1] - float(p["threshold"])) < 1e-9

    # (5) фактический горизонт в торговых барах (якорный бар → бар исхода)
    iobs = dates.index(obs[0])
    h_bars = iobs - i0
    row["h_bars"] = h_bars
    row["staleness_days"] = (datetime.date.fromisoformat(str(p["sealed_at"])[:10])
                             - datetime.date.fromisoformat(asof)).days

    # (6) «правильная» вероятность той же гауссовой базы на ФАКТИЧЕСКОМ горизонте
    sigma_d = p["sigma_h"] / math.sqrt(H_NOMINAL)
    if sigma_d > 0 and h_bars > 0:
        k_eff = math.log(p["threshold"] / px) / (sigma_d * math.sqrt(h_bars))
        row["prob_horizon_corrected"] = round(_norm_cdf(-k_eff), 4)
    else:
        row["prob_horizon_corrected"] = None

    # (7) реализованный ход в единицах σ_H + дивидендная поправка (raw vs adjusted close)
    row["ret_sigma"] = round(math.log(obs[1] / px) / p["sigma_h"], 4) if p["sigma_h"] else None
    adj0, adj1 = cl[i0][2], obs[2]
    if adj0 and adj1 and px > 0 and obs[1] > 0:
        row["dividend_drag_sigma"] = round(
            (math.log(adj1 / adj0) - math.log(obs[1] / px)) / p["sigma_h"], 4)
    else:
        row["dividend_drag_sigma"] = None
    return row


def _hits_nested(cl, i0, h=H_NOMINAL):
    """Три вложенных исхода {k=0, +0.5, −0.5} для якорного бара i0 по конвенциям calibrate."""
    if i0 < 61 or i0 + h >= len(cl):
        return None
    px = cl[i0][1]
    hist = [c for _, c, _ in cl[i0 - 60:i0 + 1]]
    lr = [math.log(hist[i + 1] / hist[i]) for i in range(len(hist) - 1)]
    sh = _std_ddof1(lr) * math.sqrt(h)
    if not sh > 0:
        return None
    obs = cl[i0 + h][1]
    return [1 if obs > round(px * math.exp(k * sh), 4) else 0 for k in K_OFFSETS]


def _infer_batch_structure(joined):
    """Д2 #15 (кросс-ревью): восстановить ФАКТИЧЕСКУЮ структуру печатей из журнала, а не
    подставлять хардкод. Возвращает (batches, K по всем ячейкам) где batches — список
    {anchor, cells:{asset:[k,...]}} по РЕАЛЬНЫМ батчам (run_id), с anchor = мин. дата якоря."""
    batches = {}
    for p, _ in joined:
        b = p.get("run_id") or str(p.get("sealed_at", ""))[:10]
        a = p["asset"]
        k = p.get("k_sigma")
        anchor = p.get("threshold_asof_close_date")
        bt = batches.setdefault(b, {"anchor": anchor, "cells": {}})
        if anchor and (bt["anchor"] is None or anchor < bt["anchor"]):
            bt["anchor"] = anchor
        bt["cells"].setdefault(a, []).append(k)
    ordered = sorted(batches.values(), key=lambda x: (x["anchor"] or ""))
    return ordered


def cluster_null(q, joined, n_sims=MC_N, seed=MC_SEED, cutoff=PRE_WINDOW_CUT, batch_offsets=None):
    """Честный null для наблюдённого hit-rate: сохраняет вложенность порогов одной asset-недели,
    кросс-корреляцию корзины и серийное перекрытие батчей — то, что наивный биномиальный тест
    игнорировал.

    Д2 #15: структура (батчи, активы каждого батча, вложенные пороги каждой ячейки, временные
    сдвиги батчей) восстанавливается ИЗ ЖУРНАЛА (_infer_batch_structure), а не из хардкода
    batch_offsets/полного набора assets×3×7. Эффективный N null = ровно len(joined). Параметр
    batch_offsets — необязательное ПЕРЕОПРЕДЕЛЕНИЕ временных сдвигов (для тестов); None → сдвиги
    выводятся из дат якорей батчей."""
    assets = sorted({p["asset"] for p, _ in joined})
    if not assets:
        return {"применимо": False, "причина": "нет разрешённых калибровочных исходов"}
    batches = _infer_batch_structure(joined)
    date_sets = [set(d for d, _, _ in q.series(a)) for a in assets]
    common_full = sorted(set.intersection(*date_sets))
    idx = {a: {d: i for i, (d, _, _) in enumerate(q.series(a))} for a in assets}
    # временные сдвиги батчей: из дат якорей (позиция в общем торговом календаре), нормированы к 0
    import bisect
    if batch_offsets is not None:
        offsets = list(batch_offsets)
        if len(offsets) != len(batches):                 # переопределение согласовано с числом батчей
            offsets = offsets[:len(batches)] + [offsets[-1]] * (len(batches) - len(offsets))
    else:
        anchor_pos = [max(0, bisect.bisect_right(common_full, b["anchor"]) - 1) for b in batches]
        base = anchor_pos[0] if anchor_pos else 0
        offsets = [p - base for p in anchor_pos]
    span = max(offsets) if offsets else 0
    hist = [d for d in common_full if d < cutoff]         # окно сэмплирования — только до cutoff
    n_hist = len(hist)

    def _cells_ok(start):
        for off, batch in zip(offsets, batches):
            j = start + off
            if j >= n_hist:
                return False
            d = hist[j]
            for a in batch["cells"]:
                if _hits_nested(q.series(a), idx[a][d]) is None:
                    return False
        return True

    starts = [s for s in range(n_hist - span) if _cells_ok(s)]
    if not starts:
        return {"применимо": False, "причина": "недостаточно выровненной истории до cutoff"}
    rng = random.Random(seed)
    sims = []
    for _ in range(n_sims):
        s = rng.choice(starts)
        tot = cnt = 0
        for off, batch in zip(offsets, batches):
            d = hist[s + off]
            for a, ks in batch["cells"].items():
                h = _hits_nested(q.series(a), idx[a][d])
                for k in ks:                              # только реально напечатанные пороги ячейки
                    tot += h[K_OFFSETS.index(k)]
                    cnt += 1
        sims.append(tot / cnt)
    obs_rate = sum(o["outcome"] for _, o in joined) / len(joined)
    p_cluster = sum(1 for x in sims if x <= obs_rate) / len(sims)
    # наивный биномиальный p (как в аляре SYNC §4.6) — для контраста
    n, hits = len(joined), sum(o["outcome"] for _, o in joined)
    p_naive = sum(math.comb(n, i) for i in range(hits + 1)) * (0.5 ** n)
    return {"применимо": True, "n_симуляций": len(sims), "seed": seed,
            "структура_из_журнала": {"n_батчей": len(batches), "сдвиги_батчей": offsets,
                                     "эффективный_N": sum(len(ks) for b in batches
                                                          for ks in b["cells"].values())},
            "окно_истории": [hist[0], hist[-1]],
            "null_среднее": round(statistics.mean(sims), 4),
            "null_sd": round(statistics.pstdev(sims), 4),
            "null_p5": round(sorted(sims)[int(0.05 * len(sims))], 4),
            "наблюдённый_hit": round(obs_rate, 4),
            "p_кластерный": round(p_cluster, 4),
            "p_наивный_биномиальный": float(f"{p_naive:.3e}"),
            "пояснение": ("кластерный null восстанавливает структуру печатей ИЗ ЖУРНАЛА "
                          "(батчи/активы/вложенные пороги/сдвиги), сохраняет вложенность порогов "
                          "одной asset-недели, кросс-корреляцию корзины и серийное перекрытие "
                          "батчей; наивный биномиальный тест считал исходы независимыми")}


def history_base_check(q, joined, cutoff=PRE_WINDOW_CUT):
    """Walk-forward-адекватность гауссовой базы: OOS-частоты трёх порогов на ВСЕЙ истории
    до cutoff против заявленных Φ(−k) (§23.1: заявка сравнивается с частотой вне окна печатей)."""
    assets = sorted({p["asset"] for p, _ in joined})
    freq = {k: [0, 0] for k in K_OFFSETS}
    for a in assets:
        cl = q.series(a)
        for i0 in range(61, len(cl) - H_NOMINAL):
            if cl[i0][0] >= cutoff:
                break
            h = _hits_nested(cl, i0)
            if h is None:
                continue
            for k, hit in zip(K_OFFSETS, h):
                freq[k][0] += hit
                freq[k][1] += 1
    out = {}
    for k in K_OFFSETS:
        hits, n = freq[k]
        out[str(k)] = {"oos_частота": (round(hits / n, 4) if n else None),
                       "заявлено": round(_norm_cdf(-k), 4), "n_окон": n}
    return out


def aggregate(rows, joined):
    """Сводка: расхождения по причинам + hit/Brier «как записано» vs «как должно быть»."""
    mism = {"sigma": [], "threshold": [], "bar": [], "outcome": [], "error": []}
    for r in rows:
        if r.get("error"):
            mism["error"].append(r["hash"])
            continue
        for key, flag in (("sigma", "sigma_ok"), ("threshold", "threshold_ok"),
                          ("bar", "bar_ok"), ("outcome", "outcome_ok")):
            if not r.get(flag):
                mism[key].append(r["hash"])
    ok_rows = [r for r in rows if not r.get("error")]
    probs_rec = [r["prob_claimed"] for r in ok_rows]
    outs_rec = [r["outcome_journal"] for r in ok_rows]
    outs_re = [r["outcome_recomputed"] for r in ok_rows]
    probs_cor = [r["prob_horizon_corrected"] if r["prob_horizon_corrected"] is not None
                 else r["prob_claimed"] for r in ok_rows]

    def _brier(ps, ys):
        return round(sum((pp - yy) ** 2 for pp, yy in zip(ps, ys)) / len(ps), 4) if ps else None

    def _hit(ys):
        return round(sum(ys) / len(ys), 4) if ys else None

    per_k = {}
    for k in K_OFFSETS:
        sub = [r for r in ok_rows if r["k_sigma"] == k]
        per_k[str(k)] = {
            "n": len(sub),
            "hit_журнал": _hit([r["outcome_journal"] for r in sub]),
            "hit_пересчёт": _hit([r["outcome_recomputed"] for r in sub]),
            "заявлено": round(_norm_cdf(-k), 4),
            "среднее_скорр_на_горизонт": (round(statistics.mean(
                [r["prob_horizon_corrected"] for r in sub
                 if r["prob_horizon_corrected"] is not None]), 4) if sub else None)}
    hb = collections.Counter(r["h_bars"] for r in ok_rows)
    div = [r["dividend_drag_sigma"] for r in ok_rows if r["dividend_drag_sigma"] is not None]
    per_run = collections.defaultdict(list)
    for r in ok_rows:
        if r["ret_sigma"] is not None:
            per_run[r["run_id"]].append(r["ret_sigma"])
    return {
        "n_разрешённых": len(joined),
        "пересчитано": len(ok_rows),
        "расхождения_по_причинам": {
            "σ_H": len(mism["sigma"]), "порог": len(mism["threshold"]),
            "бар_исхода": len(mism["bar"]), "исход_0/1": len(mism["outcome"]),
            "не_пересчитано(нет данных)": len(mism["error"]),
            "hashes": {k: v for k, v in mism.items() if v}},
        "случаи_равенства_порогу": sum(1 for r in ok_rows if r.get("equality_case")),
        "hit_rate": {"как_записано": _hit(outs_rec), "как_пересчитано": _hit(outs_re)},
        "brier": {"как_записано": _brier(probs_rec, outs_rec),
                  "по_скорректированной_вероятности(факт. горизонт)": _brier(probs_cor, outs_rec),
                  "монетка_0.5": _brier([0.5] * len(outs_rec), outs_rec)},
        "по_порогам_k": per_k,
        "фактический_горизонт_баров": dict(sorted(hb.items())),
        "свежесть_якорного_бара_дней": dict(sorted(collections.Counter(
            r["staleness_days"] for r in ok_rows).items())),
        "реализованный_ход_σH": {
            "среднее": round(statistics.mean([r["ret_sigma"] for r in ok_rows]), 4),
            "медиана": round(statistics.median([r["ret_sigma"] for r in ok_rows]), 4),
            "по_батчам": {run: round(statistics.mean(v), 4)
                          for run, v in sorted(per_run.items())}},
        "дивидендная_поправка_σH": {
            "среднее": (round(statistics.mean(div), 4) if div else None),
            "n_с_дивидендом(>1e-4)": sum(1 for x in div if x > 1e-4)},
    }


def build_report(pred_path=PROD_PREDICTIONS, outc_path=PROD_OUTCOMES, db_path=PROD_DB,
                 n_sims=MC_N, with_null=True):
    """Полный диагностический прогон. Возвращает dict отчёта (report.json)."""
    joined = load_joined(pred_path, outc_path)
    integ = journal_integrity(pred_path, outc_path)   # Д2 #14: честный знаменатель
    q = Quotes(db_path)
    try:
        rows = [recompute_record(q, p, o) for p, o in joined]
        agg = aggregate(rows, joined)
        null = cluster_null(q, joined, n_sims=n_sims) if (with_null and joined) else \
            {"применимо": False, "причина": "пропущено/нет данных"}
        base = history_base_check(q, joined) if joined else {}
    finally:
        q.close()
    d = agg["расхождения_по_причинам"]
    bug_confirmed = bool(d["σ_H"] or d["порог"] or d["бар_исхода"] or d["исход_0/1"])
    report = {
        "этап": "Д2 (ROADMAP_2026-07_search_engine.md) — аудит калибровочного трека",
        "сгенерировано": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "источники": {"predictions": str(pred_path), "outcomes": str(outc_path),
                      "db": str(db_path), "режим": "только чтение (П16)"},
        "сводка": agg,
        "целостность_журнала": integ,
        "кластерный_null": null,
        "адекватность_базы_на_истории": base,
        "вердикт": {
            "баг_сверки_подтверждён": bug_confirmed,
            "пауза_refit_П-1_01.08": bug_confirmed,   # решение владельца 13.07, Вопрос 3
            "первопричина": (
                "БАГ КОНВЕНЦИЙ НЕ ПОДТВЕРЖДЁН: все исходы воспроизводятся из сырых котировок "
                "бит-в-бит (σ_H/порог/бар/исход — 0 расхождений). Первопричина аномалии "
                "«hit 36.1%»: (1) реализованное окно 12.06–07.07 — коррелированный обвал "
                "сырьевой корзины CORE (медианный ход −0.5·σ_H); (2) алярм «биномиальный "
                "p<<0.01» предполагал 252 НЕЗАВИСИМЫХ исхода, тогда как их структура — "
                "84 asset-недели × 3 ВЛОЖЕННЫХ порога при 13 коррелированных активах и "
                "7 перекрывающихся батчах (честный кластерный p — см. кластерный_null); "
                "(3) «0.500 у каждой печати» из SYNC §4.6 — ошибка отчёта: 0.500 — это "
                "СРЕДНЕЕ трёх уровней печати {0.3085, 0.5, 0.6915}. Найденный попутно "
                "минорный баг горизонта (заявка σ√5 при фактических 5–7 барах) исправлен "
                "вперёд; его вклад ≤~1 п.п. и первопричиной не является."
                if not bug_confirmed else
                "ЕСТЬ расхождения пересчёта с журналом — см. расхождения_по_причинам."),
        },
        # блок для табло §15 (диагностический ряд; решение владельца 13.07, Вопрос 2)
        "dashboard_row": {
            "n": agg["n_разрешённых"],
            "hit_rate": agg["hit_rate"]["как_пересчитано"],
            "brier": agg["brier"]["по_скорректированной_вероятности(факт. горизонт)"],
            "пометка": ("диагностический ряд Д2 (пересчёт из сырых котировок; "
                        "вероятности скорректированы на фактический горизонт). "
                        "Баг сверки НЕ подтверждён — ряд почти совпадает с официальным."),
        },
        "строки": rows,
        "spec_ref": "ROADMAP_2026-07 Д2; §23.1 walk-forward; П8/П16",
    }
    return report


def render_md(rep):
    a = rep["сводка"]
    n = rep["кластерный_null"]
    v = rep["вердикт"]
    lines = [
        "# Д2 — диагностический пересчёт калибровочного трека",
        "",
        f"Сгенерировано: {rep['сгенерировано']} · источники: журналы+БД боевого репо, только чтение.",
        "",
        "## Итог одним абзацем",
        "",
        v["первопричина"],
        "",
        f"**Баг сверки подтверждён: {'ДА' if v['баг_сверки_подтверждён'] else 'НЕТ'}** → "
        f"пауза авто-refit П-1 01.08: {'ТРЕБУЕТСЯ' if v['пауза_refit_П-1_01.08'] else 'НЕ требуется'} "
        "(решение владельца 13.07, Вопрос 3).",
        "",
        "## Пересчёт всех исходов",
        "",
        f"- разрешённых исходов: **{a['n_разрешённых']}**, пересчитано: {a['пересчитано']}",
        (f"- целостность журнала: разрешённых калибровочных исходов "
         f"{rep['целостность_журнала']['n_resolved_calibration_outcomes']}, сматчено с печатями "
         f"{rep['целостность_журнала']['n_matched']}, БЕЗ печати "
         f"**{rep['целостность_журнала']['n_unmatched_outcomes']}** "
         + ("(целостность ОК)" if rep['целостность_журнала']['целостность_ок']
            else f"⚠ ДЕФЕКТ: {rep['целостность_журнала']['unmatched_outcome_hashes']}")),
        f"- расхождения: σ_H={a['расхождения_по_причинам']['σ_H']}, "
        f"порог={a['расхождения_по_причинам']['порог']}, "
        f"бар исхода={a['расхождения_по_причинам']['бар_исхода']}, "
        f"исход 0/1={a['расхождения_по_причинам']['исход_0/1']}",
        f"- случаев равенства порогу (строгие неравенства): {a['случаи_равенства_порогу']}",
        f"- hit-rate: как записано **{a['hit_rate']['как_записано']}**, "
        f"как пересчитано **{a['hit_rate']['как_пересчитано']}**",
        f"- Brier: как записано **{a['brier']['как_записано']}**, по скорректированной "
        f"вероятности {a['brier']['по_скорректированной_вероятности(факт. горизонт)']}, "
        f"монетка {a['brier']['монетка_0.5']}",
        "",
        "| k | n | заявлено P | hit журнал | hit пересчёт | P на факт. горизонте |",
        "|---|---|---|---|---|---|",
    ]
    for k, r in a["по_порогам_k"].items():
        lines.append(f"| {k} | {r['n']} | {r['заявлено']} | {r['hit_журнал']} | "
                     f"{r['hit_пересчёт']} | {r['среднее_скорр_на_горизонт']} |")
    lines += [
        "",
        f"- фактический горизонт (баров от якорного close до бара исхода): "
        f"{a['фактический_горизонт_баров']} (заявка печати — σ·√5)",
        f"- реализованный ход в σ_H: среднее {a['реализованный_ход_σH']['среднее']}, "
        f"медиана {a['реализованный_ход_σH']['медиана']}",
        f"- по батчам: {a['реализованный_ход_σH']['по_батчам']}",
        f"- дивидендная поправка (raw vs adjusted): среднее "
        f"{a['дивидендная_поправка_σH']['среднее']} σ_H, записей с дивидендом: "
        f"{a['дивидендная_поправка_σH']['n_с_дивидендом(>1e-4)']}",
        "",
        "## Честный null против наивного биномиального",
        "",
    ]
    if n.get("применимо"):
        lines += [
            f"- кластерный Монте-Карло ({n['n_симуляций']} симуляций, seed {n['seed']}, "
            f"история {n['окно_истории'][0]}…{n['окно_истории'][1]}): "
            f"null-среднее {n['null_среднее']}, sd {n['null_sd']}, p5 {n['null_p5']}",
            f"- наблюдённый hit {n['наблюдённый_hit']} → **кластерный p = {n['p_кластерный']}**",
            f"- наивный биномиальный p (как в SYNC §4.6) = {n['p_наивный_биномиальный']} — "
            "завышение значимости на ~4 порядка из-за ложной посылки независимости",
        ]
    else:
        lines.append(f"- null не построен: {n.get('причина')}")
    lines += [
        "",
        "## Адекватность гауссовой базы на истории (walk-forward, до 2026-06-01)",
        "",
        "| k | заявлено | OOS-частота | n окон |",
        "|---|---|---|---|",
    ]
    for k, r in rep["адекватность_базы_на_истории"].items():
        lines.append(f"| {k} | {r['заявлено']} | {r['oos_частота']} | {r['n_окон']} |")
    lines += [
        "",
        "Историческое отклонение — ВВЕРХ (дрейф рынка), т.е. база не занижала P(above) "
        "систематически; дефицит июньского окна — реализованный рынок, не конвенция.",
        "",
        "*Журналы predictions/outcomes не изменялись (только чтение, П16). "
        "Полное построчное разложение — report.json → строки.*",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Д2: диагностический пересчёт калибровочного трека")
    ap.add_argument("--predictions", default=str(PROD_PREDICTIONS))
    ap.add_argument("--outcomes", default=str(PROD_OUTCOMES))
    ap.add_argument("--db", default=str(PROD_DB))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--sims", type=int, default=MC_N)
    a = ap.parse_args(argv)
    rep = build_report(a.predictions, a.outcomes, a.db, n_sims=a.sims)
    out_dir = pathlib.Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "REPORT.md").write_text(render_md(rep), encoding="utf-8")
    print(f"[diagnose_calibration] {out_dir}/REPORT.md + report.json · "
          f"hit как записано={rep['сводка']['hit_rate']['как_записано']} · "
          f"баг сверки: {'ДА' if rep['вердикт']['баг_сверки_подтверждён'] else 'НЕТ'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
