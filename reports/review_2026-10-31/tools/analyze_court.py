# -*- coding: utf-8 -*-
"""Связан ли вердикт суда с реальным исходом? (разбор 31.10.2026; журналы и протоколы — только чтение)

1) Исследовательский суд A1 (граф_отбор.разбор_идей: балл силы 0–5, band, критерии) → исход печати
   той же идеи (run_id + актив → predictions.hash → outcomes.outcome).
2) Денежный суд §11 (граф_отбор.суд_money: РАЗБИТА/УСТОЯЛА, балл) → исход демотированной/запечатанной печати.
Если балл силы не разделяет попадания и промахи — суд ранжирует шум.
"""
import collections
import glob
import json
import math
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(__file__).resolve().parents[1]
P = [json.loads(l) for l in open(ROOT / "journal/predictions.jsonl", encoding="utf-8") if l.strip()]
O = {o["hash"]: int(o["outcome"]) for o in (json.loads(l) for l in open(ROOT / "journal/outcomes.jsonl", encoding="utf-8") if l.strip())}
by_run_asset = collections.defaultdict(list)
for p in P:
    by_run_asset[(p.get("run_id"), p.get("asset"))].append(p)


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - s) / d, (c + s) / d)


research, money = [], []
for f in sorted(glob.glob(str(ROOT / "journal/funnel_logs/ef_2026*.json"))):
    d = json.load(open(f, encoding="utf-8"))
    g = d.get("граф_отбор") or {}
    run = d.get("run_id")
    for asset, v in (g.get("разбор_идей") or {}).items():
        if not isinstance(v, dict):
            continue
        for p in by_run_asset.get((run, asset), []):
            if p["hash"] in O:
                research.append({"run": run, "asset": asset, "score": v.get("оценка"), "band": v.get("band"),
                                 "crit": v.get("критерии") or {}, "outcome": O[p["hash"]], "p": p["probability"]})
    for asset, v in (g.get("суд_money") or {}).items():
        if not isinstance(v, dict):
            v = {"исход": str(v)}
        for p in by_run_asset.get((run, asset), []):
            if p["hash"] in O:
                money.append({"run": run, "asset": asset, "verdict": v.get("исход"), "score": v.get("балл"),
                              "outcome": O[p["hash"]], "kind": p.get("kind")})


def blk(rs):
    n = len(rs)
    k = sum(r["outcome"] for r in rs)
    lo, hi = wilson(k, n)
    return {"n": n, "hit": (k / n) if n else None, "ci95": [lo, hi]}


R = {"research_matched": len(research), "money_matched": len(money)}
g = collections.defaultdict(list)
for r in research:
    s = r["score"]
    lab = "нет" if s is None else "<2" if s < 2 else "2–2.5" if s < 2.5 else "2.5–3" if s < 3 else "3–3.5" if s < 3.5 else "≥3.5"
    g[lab].append(r)
R["research_by_score"] = {k: blk(g[k]) for k in ("<2", "2–2.5", "2.5–3", "3–3.5", "≥3.5", "нет") if g.get(k)}
g = collections.defaultdict(list)
for r in research:
    g[r["band"] or "?"].append(r)
R["research_by_band"] = {k: blk(v) for k, v in g.items()}
# по критериям: верхняя треть vs нижняя треть каждого критерия
R["research_by_criterion"] = {}
crits = sorted({c for r in research for c in r["crit"]})
for c in crits:
    vals = [r for r in research if isinstance(r["crit"].get(c), (int, float))]
    hi = [r for r in vals if r["crit"][c] >= 4]
    lo = [r for r in vals if r["crit"][c] <= 2]
    R["research_by_criterion"][c] = {"≥4": blk(hi), "≤2": blk(lo)}
# корреляция балл↔исход (точечно-бисериальная)
xs = [r["score"] for r in research if isinstance(r["score"], (int, float))]
ys = [r["outcome"] for r in research if isinstance(r["score"], (int, float))]
if len(xs) > 2:
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    R["research_corr_score_outcome"] = cov / (sx * sy) if sx and sy else None
    R["research_n_runs"] = len({r["run"] for r in research})
g = collections.defaultdict(list)
for r in money:
    g[r["verdict"] or "?"].append(r)
R["money_by_verdict"] = {k: blk(v) for k, v in g.items()}
g = collections.defaultdict(list)
for r in money:
    s = r["score"]
    lab = "нет" if s is None else "<1.5" if s < 1.5 else "1.5–2" if s < 2 else "2–2.5" if s < 2.5 else "2.5–3" if s < 3 else "≥3 (допуск)"
    g[lab].append(r)
R["money_by_score"] = {k: blk(g[k]) for k in ("<1.5", "1.5–2", "2–2.5", "2.5–3", "≥3 (допуск)", "нет") if g.get(k)}

(OUT / "court.json").write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")


def f(x):
    return "—" if x is None else f"{x:.3f}"


def tbl(d, title):
    out = [f"\n### {title}", "| срез | n | попаданий | 95% ДИ |", "|---|---|---|---|"]
    for k, b in d.items():
        out.append(f"| {k} | {b['n']} | {f(b['hit'])} | {f(b['ci95'][0])}–{f(b['ci95'][1])} |")
    return "\n".join(out)


md = ["# Суд vs исход (analyze_court.py)",
      f"\nИсследовательских вердиктов с исходом: {len(research)} (прогонов {R.get('research_n_runs')}); "
      f"денежных: {len(money)}. Корреляция балла силы с исходом: {f(R.get('research_corr_score_outcome'))}.",
      tbl(R["research_by_score"], "Исследовательский суд: балл силы → попадание"),
      tbl(R["research_by_band"], "Исследовательский суд: полоса → попадание"),
      ]
for c, v in R["research_by_criterion"].items():
    md.append(tbl({f"{c} ≥4": v["≥4"], f"{c} ≤2": v["≤2"]}, f"критерий {c}"))
md.append(tbl(R["money_by_verdict"], "Денежный суд §11: вердикт → попадание печати"))
md.append(tbl(R["money_by_score"], "Денежный суд §11: балл → попадание"))
(OUT / "COURT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
print("\n".join(md))
