# -*- coding: utf-8 -*-
"""ops/calibrate_conditional.py — ДРАЙВЕР условной калибровки переноса (этап Д3, §23.1).

Генерирует (НЕ править руками — перезаписывается):
  • knowledge/conditional_sensitivities.yaml — СПРАВОЧНЫЙ артефакт условных оценок (прецедент
    FГ2 §9.1: НЕ вход живого движка; движок считает живьём)
  • ops/reports/d3_conditional/REPORT.md / report.json — walk-forward-отчёт с провенансом

Пары: эмпирический реестр knowledge/causal_links.yaml (оба направления) + звенья компаний
cascade_chains.yaml + СОБЫТИЙНЫЕ КОНТРОЛИ из боевых вердиктов суда (XOM→EEM r²=0.0; новостные
кейсы танкеров FRO/STNG/DHT при нефтяных шоках 07.2026). Гейт Д3: измерение обязано СОСТОЯТЬСЯ
walk-forward-чисто; «перенос есть» и «переноса нет» — оба легитимные исходы (рамка 3).

Порог эпизода ЕДИНЫЙ 0.5σ·√окна (решение владельца 13.07, Вопрос 4; зеркало активации B4) и
зафиксирован ДО прогона; блок «устойчивость к порогу» (0.4σ/0.5σ/0.6σ) — ДЕМОНСТРАЦИЯ
устойчивости выводов, не выбор лучшего порога. LLM не вызывается (Инв#6).
"""
import argparse
import datetime
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib.calibration import conditional as COND       # noqa: E402
from ops import calibrate_sensitivities as CS             # noqa: E402

DB = ROOT / "storage" / "oracle.db"
OUT_YAML = ROOT / "knowledge" / "conditional_sensitivities.yaml"
REPORTS = ROOT / "ops" / "reports" / "d3_conditional"
CAUSAL = ROOT / "knowledge" / "causal_links.yaml"
CHAINS = ROOT / "knowledge" / "cascade_chains.yaml"

ROBUSTNESS_FRACS = (0.4, 0.5, 0.6)   # проверка устойчивости выводов к порогу (не подбор!)

HEADER = (
    "# СГЕНЕРИРОВАНО ops/calibrate_conditional.py (этап Д3, §23.1 честная зона walk-forward).\n"
    "# Правки руками будут перезаписаны при следующей калибровке.\n"
    "# ВНИМАНИЕ (прецедент FГ2 §9.1): это КАЛИБРОВОЧНО-СПРАВОЧНЫЙ артефакт, НЕ вход живого\n"
    "# движка — движок считает живьём (mathlib/cascade.py conditional_sensitivity /\n"
    "# mathlib/calibration/conditional.py по синхронным рядам quotes). Файл держим для\n"
    "# аудита/сверки калибровки, не как источник истины движка. Боевые ворота/ранг/seal\n"
    "# условное измерение НЕ читают до этапа Э4(в,г) (ROADMAP_2026-07_search_engine.md).\n"
)

# Событийные контроли гейта Д3 — пары из боевых вердиктов суда/новостных кейсов (SYNC 13.07):
#   • XOM.US→EEM.US — вердикт «статистический перенос отсутствует (r²=0.0)»;
#   • нефтяной комплекс → танкеры FRO/STNG/DHT (Frontline/Scorpio/DHT) — новостные кейсы
#     Иран/Ормуз 07.2026, где безусловная бета пары не измерялась вовсе.
# Источники танкерных пар: BNO.US (Brent) и USO.US (WTI) — шок-источники тех событий.
EVENT_CONTROL_PAIRS = [
    ("XOM.US", "EEM.US"),
    ("BNO.US", "FRO.US"), ("BNO.US", "STNG.US"), ("BNO.US", "DHT.US"),
    ("USO.US", "FRO.US"), ("USO.US", "STNG.US"), ("USO.US", "DHT.US"),
]


def collect_pairs():
    """Направленные пары для условной оценки: (источник, узел, категория, механизм)."""
    causal = yaml.safe_load(open(CAUSAL, encoding="utf-8")) or {}
    pairs, seen = [], set()

    def add(src, dst, cat, mech=None):
        if src == dst or (src, dst) in seen:
            return
        seen.add((src, dst))
        pairs.append({"источник": src, "узел": dst, "категория": cat, "mechanism": mech})

    for p in CS._empirical_pairs(causal):
        a, b = p["pair"]
        add(a, b, "эмпирический реестр (causal_links)", p.get("mechanism"))
        add(b, a, "эмпирический реестр (causal_links)", p.get("mechanism"))
    chains_doc = yaml.safe_load(open(CHAINS, encoding="utf-8")) or {}
    for p in CS._chain_edge_pairs(chains_doc):
        add(p["источник"], p["узел"], f"звено цепочки {p.get('chain_id')}", p.get("звено"))
    for src, dst in EVENT_CONTROL_PAIRS:
        add(src, dst, "событийный контроль (вердикты суда / новостные кейсы 07.2026)")
    return pairs


def calibrate(db=None, *, robustness=True, db_note=None):
    """Прогнать условный оцениватель по всем парам; канон — порог 0.5σ, плюс блок устойчивости."""
    db = db or DB
    pairs = collect_pairs()
    records, robust = [], []
    for p in pairs:
        rec = COND.estimate_pair_symbols(p["источник"], p["узел"], db=db)
        rec = {"категория": p["категория"], "mechanism": p.get("mechanism"), **rec}
        records.append(rec)
        if robustness:
            row = {"источник": p["источник"], "узел": p["узел"]}
            for frac in ROBUSTNESS_FRACS:
                if abs(frac - COND.SHOCK_SIGMA_FRAC) < 1e-12:
                    r = rec
                else:
                    r = COND.estimate_pair_symbols(p["источник"], p["узел"], db=db,
                                                   sigma_frac=frac)
                row[f"{frac}σ"] = {
                    "status": r.get("status"), "tier": r.get("tier"),
                    "lag": r.get("lag_selected"), "gain": r.get("gain_conditional"),
                    "n_episodes": r.get("n_episodes"),
                }
            robust.append(row)

    n_est = sum(1 for r in records if r.get("wf_established"))
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "db": str(db),
        **({"db_note": db_note} if db_note else {}),
        "метод": ("условный event-study: эпизоды |shock| ≥ "
                  f"{COND.SHOCK_SIGMA_FRAC}·σ_ист·√W (W={COND.EVENT_WINDOW_DAYS}; единый порог — "
                  "решение владельца 13.07 В4, зеркало активации B4), непересекающиеся; отклик "
                  f"цели по лагам 0..{COND.MAX_LAG}; walk-forward train={COND.TRAIN}/test={COND.TEST}"
                  f"/step={COND.STEP}; установление: train p<{COND.P_ESTABLISHED} → OOS тот же знак "
                  f"и p<{COND.P_ESTABLISHED} в ≥{COND.ESTAB_FRAC_MIN:.0%} валидных фолдов"),
        "маппинг_ярусов": {
            "A": f"wf_established и ≥{COND.TIER_A_MIN_OOS_EPISODES} OOS-эпизодов (кандидат в ярус A "
                 "по условному переносу; прецедент N≥30 §10)",
            "B": f"wf_established и {COND.TIER_B_MIN_OOS_EPISODES}..{COND.TIER_A_MIN_OOS_EPISODES - 1} "
                 "OOS-эпизодов (перенос виден, выборка мала)",
            "C": "не установлено / мало эпизодов (механизм, не подтверждён — П8)",
        },
        "n_pairs": len(records),
        "n_established": n_est,
        "n_not_established": len(records) - n_est,
        "honesty_note": ("«не установлено» — легитимный и поощряемый исход (П8/рамка 3): гейт Д3 "
                         "требует, чтобы измерение СОСТОЯЛОСЬ walk-forward-чисто, а не чтобы "
                         "перенос «нашёлся». Порог эпизода зафиксирован ДО прогона."),
        "conditional_sensitivities": records,
        "robustness": robust,
    }


def _fmt(v):
    return "—" if v is None else v


def write(result):
    REPORTS.mkdir(parents=True, exist_ok=True)
    # YAML-артефакт: без тяжёлых пофолдовых деталей (они в report.json)
    yaml_recs = []
    for r in result["conditional_sensitivities"]:
        yaml_recs.append({k: v for k, v in r.items() if k != "folds"})
    doc = {k: v for k, v in result.items() if k not in ("conditional_sensitivities", "robustness")}
    doc["conditional_sensitivities"] = yaml_recs
    OUT_YAML.write_text(HEADER + "\n" + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    (REPORTS / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

    lines = [
        "# Д3 — условная амплитуда: walk-forward-отчёт калибровки (event-study)", "",
        f"_Сгенерировано {result['generated_at']} · БД: `{result['db']}`"
        + (f" ({result['db_note']})" if result.get("db_note") else "") + " · "
        f"пар: {result['n_pairs']} · установлено: {result['n_established']} · "
        f"не установлено: {result['n_not_established']}_", "",
        "## Метод (порог зафиксирован ДО оценки целей — рамка 3)", "",
        result["метод"], "",
        f"{result['honesty_note']}", "",
        "Ограничение сетки лагов L=" + str(COND.MAX_LAG) + " торговых дней (2 недели): "
        "эмпирические лаги дневных ETF = 0, а событийный перенос дальше двух недель на дневных "
        "рядах неотличим от нового события; расширение L — отдельное решение с новым отчётом.", "",
        "## Маппинг N_эпизодов → ярус честности (фиксирован до прогона)", "",
        "| ярус | критерий |", "|---|---|",
    ]
    for tier, crit in result["маппинг_ярусов"].items():
        lines.append(f"| {tier} | {crit} |")
    lines += [
        "", "## Результаты по парам (канонический порог "
        f"{COND.SHOCK_SIGMA_FRAC}σ)", "",
        "| источник→узел | категория | статус | ярус | lag* | gain (медиана OOS) | CI95 fullsample "
        "| N эп. (OOS) | фолды est/valid | провенанс |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in result["conditional_sensitivities"]:
        ci = r.get("gain_ci95")
        lines.append(
            f"| {r.get('источник')}→{r.get('узел')} | {(r.get('категория') or '')[:34]} "
            f"| {r.get('status')} | {r.get('tier')} | {_fmt(r.get('lag_selected'))} "
            f"| {_fmt(r.get('gain_conditional'))} | {ci if ci else '—'} "
            f"| {r.get('n_episodes', 0)} ({r.get('n_episodes_oos', 0)}) "
            f"| {r.get('n_folds_established', 0)}/{r.get('n_folds_valid', 0)} "
            f"| {(r.get('провенанс') or '')[:90]} |")
    lines += [
        "", "## Устойчивость выводов к порогу эпизода (0.4σ / 0.5σ / 0.6σ)", "",
        "Демонстрация устойчивости, НЕ подбор порога (канон 0.5σ подписан владельцем 13.07):", "",
        "| источник→узел | 0.4σ | 0.5σ (канон) | 0.6σ |", "|---|---|---|---|",
    ]
    for row in result["robustness"]:
        cells = []
        for frac in ROBUSTNESS_FRACS:
            c = row[f"{frac}σ"]
            cells.append(f"{c['status']}, ярус {c['tier']}, lag {_fmt(c['lag'])}, "
                         f"gain {_fmt(c['gain'])}, N={c['n_episodes']}")
        lines.append(f"| {row['источник']}→{row['узел']} | " + " | ".join(cells) + " |")
    lines += ["", "_Полные пофолдовые детали — в report.json; YAML-артефакт "
              "knowledge/conditional_sensitivities.yaml — справочный (FГ2 §9.1)._", ""]
    (REPORTS / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Д3: условная калибровка переноса (walk-forward)")
    ap.add_argument("--db", default=str(DB), help="путь к oracle.db (чтение)")
    ap.add_argument("--db-note", default=None, help="провенанс-пометка источника данных (в отчёт)")
    ap.add_argument("--no-robustness", action="store_true", help="без блока устойчивости к порогу")
    args = ap.parse_args()
    res = calibrate(db=args.db, robustness=not args.no_robustness, db_note=args.db_note)
    write(res)
    print(f"[Д3 условная калибровка] пар {res['n_pairs']}, установлено {res['n_established']}, "
          f"не установлено {res['n_not_established']}")
    print(f"  → {OUT_YAML}")
    print(f"  → {REPORTS}/REPORT.md")
