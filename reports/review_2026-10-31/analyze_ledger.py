# -*- coding: utf-8 -*-
"""Детерминированный разбор ВСЕХ запечатанных прогнозов «Оракула» (read-only).

Читает journal/predictions.jsonl + journal/outcomes.jsonl + decisions_user.jsonl +
costs.jsonl + funnel_logs/, считает честную статистику по трекам (П8: все числа — код).
Пишет stats.json и stats.md в scratchpad. Журналы НЕ трогает.
"""
import json
import glob
import math
import collections
import pathlib
import sys

ROOT = pathlib.Path("/home/user/Oracle")
OUT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
CUTOFF_NOTE = "полные данные 2026-06-12…2026-10-31 (чекпоинт сервера 31.10)"


def load_jsonl(p):
    rows = []
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


preds = load_jsonl(ROOT / "journal" / "predictions.jsonl")
outs = load_jsonl(ROOT / "journal" / "outcomes.jsonl")
decs = load_jsonl(ROOT / "journal" / "decisions_user.jsonl")
costs = load_jsonl(ROOT / "journal" / "costs.jsonl")

out_by_hash = {}
for o in outs:
    h = o.get("hash")
    if h and o.get("outcome") in (0, 1):
        out_by_hash[h] = o

# track: как в resolve.track_for_kind — по kind
def track_of(p):
    t = p.get("track")
    if t:
        return t
    k = p.get("kind") or p.get("tag") or "?"
    return {"calibration": "calibration", "edge_forward": "edge_forward",
            "money": "money", "provisional": "provisional", "test": "test"}.get(k, k)


S = collections.defaultdict(lambda: {
    "sealed": 0, "resolved": 0, "hits": 0, "brier_sum": 0.0, "brier_base_sum": 0.0,
    "probs": [], "probs_resolved": [], "first": None, "last": None,
    "unresolved_ripe": 0, "assets": collections.Counter(), "bins": collections.defaultdict(lambda: [0, 0]),
})
CUTOFF = "2026-10-31T23:59:59+00:00"
rows_money = []
for p in preds:
    tr = track_of(p)
    s = S[tr]
    s["sealed"] += 1
    ts = p.get("sealed_at") or ""
    s["first"] = min(s["first"] or ts, ts)
    s["last"] = max(s["last"] or ts, ts)
    s["assets"][p.get("asset") or "?"] += 1
    prob = p.get("probability")
    if isinstance(prob, (int, float)):
        s["probs"].append(prob)
    o = out_by_hash.get(p.get("hash"))
    if o:
        s["resolved"] += 1
        y = int(o["outcome"])
        s["hits"] += y
        if isinstance(prob, (int, float)):
            s["probs_resolved"].append(prob)
            s["brier_sum"] += (prob - y) ** 2
            br = p.get("base_rate")
            if isinstance(br, (int, float)):
                s["brier_base_sum"] += (br - y) ** 2
            else:
                s["brier_base_sum"] += (0.5 - y) ** 2
            b = min(int(prob * 10), 9)
            s["bins"][b][0] += 1
            s["bins"][b][1] += y
        if tr == "cascade_money":
            rows_money.append({
                "asset": p.get("asset"), "dir": p.get("direction"),
                "prob": prob, "outcome": y, "sealed_at": ts[:10],
                "resolve_by": (p.get("resolve_by") or "")[:10],
            })
    else:
        if (p.get("resolve_by") or "9999") < CUTOFF:
            s["unresolved_ripe"] += 1
        if tr == "cascade_money":
            rows_money.append({
                "asset": p.get("asset"), "dir": p.get("direction"),
                "prob": prob, "outcome": None, "sealed_at": ts[:10],
                "resolve_by": (p.get("resolve_by") or "")[:10],
            })

stats = {"cutoff_note": CUTOFF_NOTE, "tracks": {}}
for tr, s in sorted(S.items()):
    n = s["resolved"]
    stats["tracks"][tr] = {
        "запечатано": s["sealed"],
        "период": f"{(s['first'] or '')[:10]}…{(s['last'] or '')[:10]}",
        "разрешено": n,
        "попаданий": s["hits"],
        "hit_rate": round(s["hits"] / n, 3) if n else None,
        "средняя_заявленная_P": round(sum(s["probs_resolved"]) / n, 3) if n else (
            round(sum(s["probs"]) / len(s["probs"]), 3) if s["probs"] else None),
        "brier": round(s["brier_sum"] / n, 4) if n else None,
        "brier_базовой_частоты": round(s["brier_base_sum"] / n, 4) if n else None,
        "skill_vs_base": round((s["brier_base_sum"] - s["brier_sum"]) / n, 4) if n else None,
        "созрело_но_не_сверено": s["unresolved_ripe"],
        "калибровка_бины_P→(N,факт)": {
            f"{b/10:.1f}–{(b+1)/10:.1f}": {"N": v[0], "факт_частота": round(v[1] / v[0], 3)}
            for b, v in sorted(s["bins"].items()) if v[0] >= 5},
        "топ_активов": s["assets"].most_common(8),
    }

# решения владельца
dec_actions = collections.Counter(d.get("action") for d in decs if d.get("action"))
motives = sum(1 for d in decs if d.get("motive"))

# расходы
cost_total = sum(c.get("cost_usd") or 0 for c in costs)
cost_by_agent = collections.Counter()
for c in costs:
    cost_by_agent[c.get("agent") or "?"] += c.get("cost_usd") or 0
stats["решения_владельца"] = {"всего": sum(dec_actions.values()), **dec_actions, "мотивов": motives}
stats["LLM_расходы_usd"] = {"всего": round(cost_total, 2),
                            "топ_агентов": [(a, round(v, 2)) for a, v in cost_by_agent.most_common(10)]}

# воронка: сколько прогонов, сколько дней с идеями
runs = sorted(glob.glob(str(ROOT / "journal" / "funnel_logs" / "*.json")))
run_days = collections.Counter()
ef_runs = 0
for rp in runs:
    name = pathlib.Path(rp).name
    if name.startswith("ef_") and "__" not in name and "model_swaps" not in name:
        ef_runs += 1
stats["воронка"] = {"файлов_протоколов": len(runs), "сводных_ef_прогонов": ef_runs}

(OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")

md = ["# Статистика прогнозов «Оракула» (детерминированный пересчёт, " + CUTOFF_NOTE + ")", ""]
for tr, t in stats["tracks"].items():
    md.append(f"## Трек `{tr}`")
    for k, v in t.items():
        if k in ("калибровка_бины_P→(N,факт)", "топ_активов"):
            continue
        md.append(f"- {k}: {v}")
    if t["калибровка_бины_P→(N,факт)"]:
        md.append("- калибровка (бины заявленной P → фактическая частота):")
        for b, v in t["калибровка_бины_P→(N,факт)"].items():
            md.append(f"  - P {b}: N={v['N']}, факт {v['факт_частота']}")
    md.append("")
md.append("## Денежный трек — все печати поимённо")
md.append("| актив | напр. | P заявл. | исход | печать | резолв |")
md.append("|---|---|---|---|---|---|")
for r in sorted(rows_money, key=lambda x: x["sealed_at"]):
    oc = {1: "✅ сбылся", 0: "❌ не сбылся", None: "⏳ не сверен в клоне"}[r["outcome"]]
    md.append(f"| {r['asset']} | {r['dir']} | {r['prob']} | {oc} | {r['sealed_at']} | {r['resolve_by']} |")
md.append("")
md.append(f"## Решения владельца: {dict(dec_actions)} (мотивов: {motives})")
md.append(f"## LLM-расходы: ${round(cost_total, 2)}")
(OUT / "stats.md").write_text("\n".join(md), encoding="utf-8")
print("OK; треки:", {tr: (t['запечатано'], t['разрешено'], t['hit_rate']) for tr, t in stats['tracks'].items()})
