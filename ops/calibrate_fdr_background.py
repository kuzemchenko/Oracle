#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/calibrate_fdr_background.py — перегенерация фона FDR под ОТКРЫТУЮ вселенную + df t-хвоста
per-instrument (этап Д1 программы «Поисковый движок», ROADMAP 2026-07, подпись владельца 13.07).

Что делает (детерминированно, БЕЗ LLM, боевая БД — только чтение):
  1. background_metrics (ret/absret/dvol) по ВСЕМ символам sealable_universe с историей
     ≥ MIN_BG_BARS; символы короче — честная запись «нет фона» (П8), не молчаливый пропуск.
  2. tail_df: walk-forward подбор df Стьюдента-t per-instrument по историческим сканным z
     (mathlib/calibration/tail_df.py; все пороги процедуры зафиксированы там ДО прогона).
     Непинящиеся инструменты → фолбэк из пула z (значение из расчёта, не из головы).
  3. Проверка на look-ahead: df пересчитывается ещё раз на данных ≤ STABILITY_CUTOFF
     (до начала replay-окна Д1 21.06–12.07) — расхождения видны в отчёте.
  4. Перезапись config/thresholds.yaml: заменяется ТОЛЬКО fdr.background_metrics,
     добавляется fdr.tail_df и обновляется заголовок-провенанс; все прочие секции
     (fdr.* прочие ключи, timing, manipulation, …) сохраняются БАЙТ-В-БАЙТ (сплайс текста).
  5. Отчёты: ops/reports/fdr_background/{REPORT.md, report.json}.

q_value_max=0.1 НЕ трогается (решение владельца B5/§6). Журналы journal/* не пишутся.

Запуск (боевая БД по абсолютному пути, только чтение):
  python3 ops/calibrate_fdr_background.py --db /home/oracle/oracle/storage/oracle.db --write
"""
import sys
import json
import math
import argparse
import pathlib
import sqlite3
import datetime

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib.calibration import backgrounds as bg          # noqa: E402
from mathlib.calibration import loader                     # noqa: E402
from mathlib.calibration import tail_df as TD              # noqa: E402
from orchestrator import universe_resolver as U            # noqa: E402

REPORTS = ROOT / "ops" / "reports" / "fdr_background"
THRESHOLDS = ROOT / "config" / "thresholds.yaml"

# ── Пороги драйвера: зафиксированы 2026-07-13 ДО прогона replay-сравнения (рамка 3) ──
MIN_BG_BARS = 260          # ≥ ~1 торговый год истории для фона; короче — «нет фона» (П8)
STABILITY_CUTOFF = "2026-06-20"   # день ПЕРЕД replay-окном Д1 (21.06–12.07): гард от look-ahead
LEGACY_DF = {"ret_z_20": 5, "vol_z_log_20": 6, "vol_z_20": 3}   # константы F2#19 (8e901ec)

NOW = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect_ro(db_path):
    """Боевая БД — ТОЛЬКО чтение (рамка Д1): sqlite URI mode=ro, запись невозможна."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)


def _load_series(con, symbol, cutoff=None):
    """loader.Series по возрастанию даты; cutoff — date <= cutoff (для гарда устойчивости)."""
    q = ("SELECT date, open, high, low, close, adjusted_close, volume FROM quotes "
         "WHERE symbol=?" + (" AND date<=?" if cutoff else "") + " ORDER BY date ASC")
    rows = con.execute(q, (symbol, cutoff) if cutoff else (symbol,)).fetchall()
    return loader.Series(symbol, rows)


def _round_bg(b):
    if b.get("insufficient"):
        return {"insufficient": True, "n": b["n"]}
    return {"std": round(b["std"], 6), "mad_sigma": round(b["mad_sigma"], 6),
            "q99": round(b["q99"], 6), "q01": round(b["q01"], 6), "n": b["n"]}


def _z_series(ser_adj):
    """Сканные z инструмента: ret_z_20 (простые доходности adjusted) и vol_z_log_20 (сырой объём)."""
    return {"ret_z_20": TD.scan_ret_z_series(ser_adj.adj),
            "vol_z_log_20": TD.scan_vol_z_series(ser_adj.volume)}


def calibrate_universe(con, symbols, cutoff=None):
    """Полный проход по вселенной: фон + df. Возвращает (backgrounds, tail, z_pool)."""
    backgrounds, tail, z_pool = {}, {}, {"ret_z_20": [], "vol_z_log_20": []}
    for sym in symbols:
        ser = _load_series(con, sym, cutoff)
        n_bars = len(ser)
        if n_bars < MIN_BG_BARS:
            backgrounds[sym] = {"insufficient_history": True, "n_bars": n_bars,
                                "note": f"нет фона (П8): история {n_bars} < {MIN_BG_BARS} баров"}
        else:
            adj = loader.adjusted_view(ser)
            backgrounds[sym] = {m: _round_bg(bg.background(bg.metric_series(adj, m)))
                                for m in ("ret", "absret", "dvol")}
        adj = loader.adjusted_view(ser)
        zs = _z_series(adj)
        tail[sym] = {}
        for metric, z in zs.items():
            res = TD.calibrate_instrument(z)
            tail[sym][metric] = res
            if res["n"] >= TD.TRAIN_SIZE + TD.TEST_SIZE:
                z_pool[metric].append((sym, z))
    return backgrounds, tail, z_pool


def build_tail_section(tail, z_pool, tail_stab, window, n_universe):
    """Секция fdr.tail_df для thresholds.yaml + фолбэк из пула (детерминированный порядок)."""
    fallback = {}
    fallback_detail = {}
    for metric in ("ret_z_20", "vol_z_log_20"):
        pool = [z for _, z in sorted(z_pool[metric], key=lambda t: t[0])]
        fb = TD.pooled_fallback_df(pool)
        fallback_detail[metric] = fb
        fallback[metric] = fb["df"] if fb["df"] is not None else LEGACY_DF[metric]
    per_instrument, unpinned = {}, {}
    for sym in sorted(tail):
        row, why = {}, {}
        for metric, res in tail[sym].items():
            if res.get("pinned"):
                row[metric] = res["df"]
            else:
                why[metric] = res.get("reason", "не пинится")
        if row:
            per_instrument[sym] = row
        if why:
            unpinned[sym] = why
    n_pinned = {m: sum(1 for s in per_instrument.values() if m in s)
                for m in ("ret_z_20", "vol_z_log_20")}
    stability = {}
    for sym in sorted(tail):
        for metric in ("ret_z_20", "vol_z_log_20"):
            full = tail[sym][metric]
            stab = (tail_stab.get(sym) or {}).get(metric, {})
            if full.get("pinned") and stab.get("pinned") and full["df"] != stab["df"]:
                stability.setdefault(sym, {})[metric] = {
                    "df_полная_история": full["df"], f"df_до_{STABILITY_CUTOFF}": stab["df"]}
    section = {
        "provenance": {
            "script": "ops/calibrate_fdr_background.py",
            "generated_at": NOW,
            "data_window": window,
            "n_symbols_universe": n_universe,
            "n_pinned": n_pinned,
            "method": ("walk-forward подбор df t-нуля по историческим сканным z "
                       "(mathlib/calibration/tail_df.py): train→fit по хвостовым частотам "
                       f"{list(TD.FIT_THRESHOLDS)}→OOS-проверка |z|>2,|z|>3; "
                       f"train={TD.TRAIN_SIZE}, test={TD.TEST_SIZE}; пороги зафиксированы "
                       "ДО replay-сравнения (рамка 3 программы)"),
            "fallback_source": ("пул сканных z всех инструментов с достаточной историей "
                                "(детерминированный порядок по символу); НЕ из головы (П8)"),
            "stability_cutoff": STABILITY_CUTOFF,
            "stability_divergence_n": len(stability),
            "report": "ops/reports/fdr_background/",
        },
        "fallback": {**{k: float(v) for k, v in fallback.items()},
                     "vol_z_20": LEGACY_DF["vol_z_20"],
                     "note": ("ret_z_20/vol_z_log_20 — df пула (см. отчёт); vol_z_20 — редкая "
                              "ветка сырого объёма (когда лог-метрики нет), оставлена константа "
                              "F2#19: отдельно не калибровалась")},
        "per_instrument": per_instrument,
        "unpinned": unpinned or None,
    }
    return section, fallback_detail, stability


# ── Сплайс thresholds.yaml: прочие секции байт-в-байт ────────────────────────────────

def splice_thresholds(old_text, bg_out, tail_section, window, n_universe, n_no_bg):
    """Заменяет заголовок-провенанс и блок fdr.background_metrics (+добавляет fdr.tail_df).
    ВСЁ остальное — байт-в-байт из старого файла. Возвращает новый текст."""
    lines = old_text.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith("#"):
        i += 1
    body = lines[i:]
    try:
        bm = body.index("  background_metrics:")
    except ValueError:
        raise SystemExit("сплайс: не найден блок '  background_metrics:' в thresholds.yaml")
    try:
        tm = body.index("timing:")
    except ValueError:
        raise SystemExit("сплайс: не найдена секция 'timing:' в thresholds.yaml")
    if tm < bm:
        raise SystemExit("сплайс: неожиданный порядок секций thresholds.yaml")
    block = yaml.safe_dump({"background_metrics": bg_out, "tail_df": tail_section},
                           allow_unicode=True, sort_keys=False, default_flow_style=False)
    block_lines = ["  " + ln if ln.strip() else ln for ln in block.rstrip("\n").split("\n")]
    header = [
        "# СЕКЦИИ fdr.background_metrics + fdr.tail_df СГЕНЕРИРОВАНЫ ops/calibrate_fdr_background.py",
        "# (этап Д1 «Поисковый движок», walk-forward §23.1; боевая БД читалась read-only).",
        f"# Дата генерации: {NOW}. Окно данных: {window['from']}…{window['to']}.",
        f"# Вселенная: {n_universe} символов sealable_universe (открытая, не 8 ядра); "
        f"без фона (история<{MIN_BG_BARS} баров): {n_no_bg} — помечены в background_metrics (П8).",
        "# ПРОЧИЕ секции (fdr.* остальное, timing, manipulation, …) — БАЙТ-В-БАЙТ из генерации",
        "# ops/calibrate_week4.py 2026-06-11 (train_window/calibrated_at ниже описывают ИХ).",
        "# Правки руками будут перезаписаны при следующем прогоне калибровки.",
    ]
    return "\n".join(header + body[:bm] + block_lines + body[tm:])


# ── Отчёты ────────────────────────────────────────────────────────────────────────

def write_reports(backgrounds, tail, tail_section, fallback_detail, stability,
                  window, symbols, db_path):
    REPORTS.mkdir(parents=True, exist_ok=True)
    per = tail_section["per_instrument"]
    unp = tail_section["unpinned"] or {}
    no_bg = {s: b for s, b in backgrounds.items() if b.get("insufficient_history")}
    obj = {
        "script": "ops/calibrate_fdr_background.py",
        "generated_at": NOW,
        "db": str(db_path),
        "db_access": "read-only (sqlite URI mode=ro)",
        "data_window": window,
        "min_bg_bars": MIN_BG_BARS,
        "n_symbols_universe": len(symbols),
        "n_background": len(backgrounds) - len(no_bg),
        "no_background": no_bg,
        "tail_df": {
            "thresholds_fixed_before_replay": {
                "df_grid": list(TD.DF_GRID), "fit_thresholds": list(TD.FIT_THRESHOLDS),
                "oos_thresholds": list(TD.OOS_THRESHOLDS), "train": TD.TRAIN_SIZE,
                "test": TD.TEST_SIZE, "min_folds": TD.MIN_FOLDS,
                "min_oos_ok_fraction": TD.MIN_OOS_OK_FRACTION, "oos_alpha": TD.OOS_ALPHA,
                "stability_criterion": ("v2 (пересмотр 13.07 ДО replay-сравнения): OOS-валидация "
                                        "МЕДИАННОГО df; v1 df-ratio отклонял шум плоской области "
                                        "потерь — обоснование в шапке mathlib/calibration/tail_df.py")},
            "legacy_df_constants": LEGACY_DF,
            "fallback": tail_section["fallback"],
            "fallback_detail": {m: {k: v for k, v in d.items()}
                                for m, d in fallback_detail.items()},
            "per_instrument": per,
            "unpinned": unp,
            "stability_cutoff": STABILITY_CUTOFF,
            "stability_divergence": stability,
            "detail": {s: {m: {k: v for k, v in r.items() if k != "folds"}
                           for m, r in t.items()} for s, t in tail.items()},
        },
    }
    (REPORTS / "report.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=1, default=float), encoding="utf-8")

    df_ret = [v["ret_z_20"] for v in per.values() if "ret_z_20" in v]
    df_vol = [v["vol_z_log_20"] for v in per.values() if "vol_z_log_20" in v]
    L = ["# Отчёт Д1: фон FDR под открытую вселенную + df t-хвоста per-instrument\n",
         f"Сгенерировано: {NOW} — `ops/calibrate_fdr_background.py` (детерминированно, без LLM).",
         f"БД: `{db_path}` (ТОЛЬКО чтение, sqlite mode=ro). Окно данных: "
         f"{window['from']}…{window['to']}.\n",
         "## Пороги процедуры (зафиксированы ДО replay-сравнения, рамка 3)\n",
         f"- сетка df: {list(TD.DF_GRID)}",
         f"- пороги подбора (хвостовые частоты): {list(TD.FIT_THRESHOLDS)}; "
         f"OOS-проверка: |z|>{TD.OOS_THRESHOLDS[0]}, |z|>{TD.OOS_THRESHOLDS[1]}",
         f"- walk-forward: train {TD.TRAIN_SIZE} / test {TD.TEST_SIZE}, фолдов ≥ {TD.MIN_FOLDS}",
         f"- пин (критерий v2, пересмотр 13.07 ДО replay-сравнения — обоснование в шапке "
         f"mathlib/calibration/tail_df.py): МЕДИАННЫЙ фолдовый df проходит OOS-проверку "
         f"хвостовых частот в ≥ {TD.MIN_OOS_OK_FRACTION:.0%} фолдов",
         f"- порог истории фона: ≥ {MIN_BG_BARS} баров (~1 торговый год); короче — «нет фона» (П8)",
         f"- гард look-ahead: df дополнительно пересчитан на данных ≤ {STABILITY_CUTOFF} "
         "(до replay-окна 21.06–12.07); расхождения — в таблице ниже\n",
         "## Итоги\n",
         f"- вселенная (sealable, откр.): **{len(symbols)}** символов; фон посчитан по "
         f"**{len(backgrounds) - len(no_bg)}**, «нет фона» — **{len(no_bg)}**",
         f"- df ret_z_20: пин у **{len(df_ret)}** символов; медиана {np.median(df_ret) if df_ret else '—'}, "
         f"мин {min(df_ret) if df_ret else '—'}, макс {max(df_ret) if df_ret else '—'}",
         f"- df vol_z_log_20: пин у **{len(df_vol)}**; медиана {np.median(df_vol) if df_vol else '—'}, "
         f"мин {min(df_vol) if df_vol else '—'}, макс {max(df_vol) if df_vol else '—'}",
         f"- фолбэк (пул): ret_z_20 = **{tail_section['fallback']['ret_z_20']}** "
         f"(n={fallback_detail['ret_z_20']['n']}), vol_z_log_20 = "
         f"**{tail_section['fallback']['vol_z_log_20']}** (n={fallback_detail['vol_z_log_20']['n']}); "
         f"vol_z_20 (сырой, редкая ветка) — константа F2#19 = {LEGACY_DF['vol_z_20']}",
         f"- было (константы F2#19): ret 5 / лог-объём 6 / сырой объём 3",
         f"- расхождений пина при отсечке ≤ {STABILITY_CUTOFF}: **{len(stability)}** "
         + ("(см. таблицу)" if stability else "(look-ahead-эффекта replay-окна на df нет)") + "\n"]
    if stability:
        L.append("## Расхождения df: полная история vs ≤ " + STABILITY_CUTOFF + "\n")
        L.append("| Символ | метрика | df (полная) | df (≤ отсечки) |\n|---|---|---|---|")
        for sym, mm in sorted(stability.items()):
            for metric, d in mm.items():
                L.append(f"| {sym} | {metric} | {d['df_полная_история']} | "
                         f"{d[f'df_до_{STABILITY_CUTOFF}']} |")
        L.append("")
    if no_bg:
        L.append("## Символы без фона (П8, не молчаливый пропуск)\n")
        L.append("| Символ | баров |\n|---|---|")
        for sym, b in sorted(no_bg.items()):
            L.append(f"| {sym} | {b['n_bars']} |")
        L.append("")
    if unp:
        L.append("## Инструменты/метрики без устойчивого пина (→ фолбэк пула)\n")
        L.append("| Символ | метрика | причина |\n|---|---|---|")
        for sym, mm in sorted(unp.items()):
            for metric, why in mm.items():
                L.append(f"| {sym} | {metric} | {why} |")
        L.append("")
    L.append("## Per-instrument df (пины)\n")
    L.append("| Символ | ret_z_20 | vol_z_log_20 |\n|---|---|---|")
    for sym in sorted(per):
        v = per[sym]
        L.append(f"| {sym} | {v.get('ret_z_20', '— (фолбэк)')} | {v.get('vol_z_log_20', '— (фолбэк)')} |")
    L.append("\n---\n*Артефакты: config/thresholds.yaml (fdr.background_metrics + fdr.tail_df), "
             "report.json рядом. q_value_max=0.1 не тронут (B5/§6).*\n")
    (REPORTS / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Д1: фон FDR открытой вселенной + tail_df (read-only БД)")
    ap.add_argument("--db", default=str(ROOT / "storage" / "oracle.db"))
    ap.add_argument("--write", action="store_true", help="переписать config/thresholds.yaml")
    args = ap.parse_args()
    db_path = pathlib.Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"нет БД: {db_path}")

    con = _connect_ro(db_path)
    try:
        symbols = U.sealable_universe(con=con)
        dates = con.execute("SELECT MIN(date), MAX(date) FROM quotes").fetchone()
        window = {"from": dates[0], "to": dates[1]}
        print(f"Вселенная: {len(symbols)} символов; окно {window['from']}…{window['to']}")
        print("Проход 1/2: полная история (фон + df)…")
        backgrounds, tail, z_pool = calibrate_universe(con, symbols)
        print(f"Проход 2/2: гард устойчивости ≤ {STABILITY_CUTOFF} (только df)…")
        _, tail_stab, _ = calibrate_universe(con, symbols, cutoff=STABILITY_CUTOFF)
    finally:
        con.close()

    tail_section, fallback_detail, stability = build_tail_section(
        tail, z_pool, tail_stab, window, len(symbols))
    write_reports(backgrounds, tail, tail_section, fallback_detail, stability,
                  window, symbols, db_path)
    no_bg = sum(1 for b in backgrounds.values() if b.get("insufficient_history"))
    print(f"Фон: {len(backgrounds) - no_bg} символов; нет фона: {no_bg}")
    print(f"Пины df: ret {tail_section['provenance']['n_pinned']['ret_z_20']}, "
          f"vol {tail_section['provenance']['n_pinned']['vol_z_log_20']}; "
          f"фолбэк ret={tail_section['fallback']['ret_z_20']}, "
          f"vol={tail_section['fallback']['vol_z_log_20']}")
    print(f"Расхождений df при отсечке ≤ {STABILITY_CUTOFF}: {len(stability)}")
    if args.write:
        old = THRESHOLDS.read_text(encoding="utf-8")
        new = splice_thresholds(old, backgrounds, tail_section, window, len(symbols), no_bg)
        THRESHOLDS.write_text(new, encoding="utf-8")
        print(f"Записан {THRESHOLDS} (прочие секции — байт-в-байт)")
    else:
        print("Без --write: thresholds.yaml не тронут (dry-run)")
    print(f"Отчёты: {REPORTS}/REPORT.md, report.json")


if __name__ == "__main__":
    main()
