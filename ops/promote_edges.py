# -*- coding: utf-8 -*-
"""ops/promote_edges.py — ДРАЙВЕР форвард-промоушена каскадных рёбер в ярус A (решение 28.06.2026).

Читает запечатанные провизорные прогнозы (journal/predictions.jsonl) + их исходы
(journal/outcomes.jsonl), джойнит по hash, атрибутирует ОДНОЗВЕННЫЕ пути (cascade_path len==1)
своему ребру и считает форвард-скилл по ребру (mathlib.calibration.forward_promotion, §10: N≥30 +
значимость). Ребро, прошедшее гейт, становится ярус-A established с надёжностью из форвард-скилла.

НУЛЕВАЯ АВТОНОМИЯ (П16, петля §25 = только предложения):
  • по умолчанию — DRY: пишет ПРЕДЛОЖЕНИЯ в ops/reports/promotions/ (человеко+машинный отчёт),
    knowledge/forward_promotions.yaml НЕ трогает;
  • с флагом --apply — ПРИМЕНЯЕТ: перезаписывает knowledge/forward_promotions.yaml (ежемесячно,
    рукой владельца). Файл генерируемый — правки руками будут перезаписаны.

Только детерминированный код (инвариант #6). LLM здесь нет.
"""
import argparse
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mathlib import sealing as SEAL                       # noqa: E402
from mathlib.calibration import forward_promotion as FP   # noqa: E402
from orchestrator import resolve as RES                   # noqa: E402

OUT_YAML = ROOT / "knowledge" / "forward_promotions.yaml"
REPORTS = ROOT / "ops" / "reports" / "promotions"

HEADER = (
    "# СГЕНЕРИРОВАНО ops/promote_edges.py --apply (форвард-промоушен рёбер, решение 28.06.2026).\n"
    "# Правки руками будут перезаписаны. Ребро здесь = ярус-A established, ЗАРАБОТАННЫЙ корректными\n"
    "# запечатанными форвард-прогнозами (N≥30, значимый скилл, §10) — НЕ исторической бетой.\n"
)


def collect_rows(predictions_path=None, outcomes_path=None):
    """Однозвенные провизорные прогнозы с известным исходом → строки для forward_promotion.
    Возвращает (rows, stats). Многозвенные пути пропускаем (исход композитный, не атрибутируется ребру)."""
    preds = SEAL.read_predictions(predictions_path)
    outs = {o["hash"]: o for o in RES.read_outcomes(outcomes_path) if o.get("hash")}
    rows, multi, no_path, pending = [], 0, 0, 0
    # B4 (§R4.5): фарм-поток edge_forward — основной корм промоушена (однозвенные по построению);
    # провизорные однозвенные прогнозы выдачи остаются вторым источником, как раньше.
    kinds = tuple(RES.PROVISIONAL_KINDS) + tuple(RES.EDGE_FORWARD_KINDS)
    for p in preds:
        if p.get("kind") not in kinds:
            continue
        path = p.get("cascade_path") or []
        if not path:
            no_path += 1
            continue
        if len(path) != 1:
            multi += 1
            continue
        o = outs.get(p.get("hash"))
        if not o or o.get("outcome") not in (0, 1):
            pending += 1
            continue
        e = path[0]
        rows.append({
            "edge_key": FP.edge_key(e.get("from"), e.get("to"), e.get("lag")),
            "from": e.get("from"), "to": e.get("to"), "lag": int(e.get("lag") or 0),
            # П-1 (подпись 09.07): скилл ребра меряем по СЫРОЙ уверенности модели (probability_raw,
            # до сжатия к базовой частоте) — иначе сжатая официальная шкала (λ=0 → p≈p0) даёт
            # BSS≈0 и промоушен структурно никогда не срабатывает. Официальный Brier табло — по
            # сжатой; скилл/промоушен — по сырой. Легаси-записи без raw — как раньше.
            "probability": (p.get("probability_raw")
                            if p.get("probability_raw") is not None else p.get("probability")),
            "outcome": int(o["outcome"]),
            "beta_fullsample": e.get("beta_fullsample"),
            # identity СОБЫТИЯ для меж-трекового дедупа корма (stage-review B4 high-а)
            "_bet": (p.get("asset"), p.get("direction"), p.get("threshold"), p.get("resolve_by")),
        })
    # stage-review B4 (high-а): одна и та же ставка, запечатанная в ДВУХ треках (cascade_provisional
    # выдачи + edge_forward фарм-потока — дедуп треков сознательно внутри-трековый), — это ОДНО
    # рыночное событие. В корм промоушена оно обязано войти ОДИН раз, иначе N ребра надувается
    # двойным счётом зависимых исходов (биномтест §10 считает их независимыми). Keep-first — порядок
    # журнала детерминирован. Дедуп ТОЛЬКО при полной identity ставки: равенство по отсутствующим
    # полям не выдумываем (П8) — неполные записи различает hash-джойн исходов.
    seen, deduped, cross_dupes = set(), [], 0
    for r in rows:
        bet = r.pop("_bet")
        if any(v is None for v in bet):
            deduped.append(r)
            continue
        key = (r["edge_key"],) + bet
        if key in seen:
            cross_dupes += 1
            continue
        seen.add(key)
        deduped.append(r)
    rows = deduped
    stats = {"провизорных_исходов_однозвенных": len(rows), "многозвенных_пропущено": multi,
             "без_cascade_path": no_path, "ещё_pending": pending,
             "дубль_событий_между_треками": cross_dupes}
    return rows, stats


def evaluate(predictions_path=None, outcomes_path=None, min_outcomes=FP.MIN_OUTCOMES):
    rows, stats = collect_rows(predictions_path, outcomes_path)
    decisions = FP.promote_all(rows, min_outcomes=min_outcomes)
    # обогащаем from/to/lag (для записи ребра) из первой строки каждого ключа
    meta = {}
    for r in rows:
        meta.setdefault(r["edge_key"], {"from": r["from"], "to": r["to"], "lag": r["lag"]})
    promotions = {}
    for k, d in decisions.items():
        promotions[k] = {**meta.get(k, {}), **d}
    n_promote = sum(1 for d in promotions.values() if d.get("promote"))
    return {"stats": stats, "n_edges": len(promotions), "n_promote": n_promote,
            "promotions": promotions}


def write_report(result):
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    s = result["stats"]
    lines = ["# Форвард-промоушен каскадных рёбер (предложения, §10)", "",
             f"_рёбер оценено: {result['n_edges']} · к промоушену: {result['n_promote']} · "
             f"однозвенных исходов: {s['провизорных_исходов_однозвенных']} · "
             f"многозвенных пропущено: {s['многозвенных_пропущено']} · "
             f"pending: {s['ещё_pending']}_", "",
             "Промоушен применяется ТОЛЬКО `ops/promote_edges.py --apply` (рукой владельца, §25).", "",
             "| ребро | lag | N | hit-rate | Brier | p-value | → ярус A? | надёжн. | причина |",
             "|---|---|---|---|---|---|---|---|---|"]
    for k, d in sorted(result["promotions"].items(),
                       key=lambda kv: (not kv[1].get("promote"), -(kv[1].get("n") or 0))):
        mark = "✅ ДА" if d.get("promote") else "—"
        lines.append(f"| {d.get('from')}→{d.get('to')} | {d.get('lag')} | {d.get('n')} "
                     f"| {d.get('hit_rate')} | {d.get('brier')} | {d.get('p_value')} | {mark} "
                     f"| {d.get('reliability')} | {(d.get('причина') or '')[:60]} |")
    (REPORTS / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_apply(result):
    """Перезаписывает knowledge/forward_promotions.yaml — только прошедшие гейт рёбра (promote=True)."""
    promoted = {k: v for k, v in result["promotions"].items() if v.get("promote")}
    doc = {"n_promoted": len(promoted), "min_outcomes": FP.MIN_OUTCOMES,
           "promotions": promoted}
    OUT_YAML.write_text(HEADER + "\n" + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description="форвард-промоушен каскадных рёбер (§10)")
    ap.add_argument("--apply", action="store_true",
                    help="ПРИМЕНИТЬ: перезаписать knowledge/forward_promotions.yaml (иначе только отчёт)")
    ap.add_argument("--min-outcomes", type=int, default=FP.MIN_OUTCOMES)
    args = ap.parse_args(argv)
    result = evaluate(min_outcomes=args.min_outcomes)
    write_report(result)
    print(f"[промоушен рёбер] оценено {result['n_edges']}, к промоушену {result['n_promote']}")
    print(f"  → {REPORTS}/REPORT.md")
    if args.apply:
        write_apply(result)
        print(f"  ПРИМЕНЕНО → {OUT_YAML} ({result['n_promote']} рёбер)")
    else:
        print("  DRY: forward_promotions.yaml не изменён (нужен --apply, §25 — рукой владельца)")
    return result


if __name__ == "__main__":
    main()
