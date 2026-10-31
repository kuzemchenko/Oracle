# -*- coding: utf-8 -*-
"""Разбор 3,5 месяцев автономной работы (15.07→31.10.2026): где у системы реальный сигнал.

ТОЛЬКО ЧТЕНИЕ журналов (П16). LLM нет. Детерминирован.
Вопросы:
  (a) калибровочный трек — умение или просто хорошо выставленная базовая частота? (Brier skill vs
      климатология, разложение по k_sigma, по месяцам);
  (b) провизорный каскадный трек — предсказывает ли reliability_r2 / глубина цепочки / ярус попадание
      (гипотеза 15.07: «ближнее звено с высоким r² попадает чаще»);
  (d) edge_forward (B4) — есть ли рёбра со скиллом;
  + денежный трек как есть.
Пишет REPORT.md + report.json рядом (reports/review_2026-10-31/).
"""
import collections
import json
import math
import pathlib
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(__file__).resolve().parents[1]
P = [json.loads(l) for l in open(ROOT / "journal/predictions.jsonl", encoding="utf-8") if l.strip()]
O = [json.loads(l) for l in open(ROOT / "journal/outcomes.jsonl", encoding="utf-8") if l.strip()]
by_hash = {p["hash"]: p for p in P}
rows = []
for o in O:
    p = by_hash.get(o["hash"])
    if not p:
        continue
    r = dict(p)
    r["outcome"] = int(o["outcome"])
    r["month"] = (p.get("sealed_at") or "")[:7]
    rows.append(r)


def brier(rs):
    return sum((r["probability"] - r["outcome"]) ** 2 for r in rs) / len(rs) if rs else None


def hit(rs):
    return sum(r["outcome"] for r in rs) / len(rs) if rs else None


def skill(rs):
    """Brier skill score против климатологии (постоянная P = средняя частота исходов выборки).
    >0 — прогнозы несут информацию сверх базовой частоты; ≤0 — нет."""
    if not rs:
        return None
    h = hit(rs)
    clim = sum((h - r["outcome"]) ** 2 for r in rs) / len(rs)
    b = brier(rs)
    return 1 - b / clim if clim > 0 else None


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - s) / d, (c + s) / d)


def block(name, rs):
    n = len(rs)
    lo, hi = wilson(sum(r["outcome"] for r in rs), n)
    return {"n": n, "hit": hit(rs), "hit_ci95": [lo, hi], "brier": brier(rs), "skill_vs_clim": skill(rs),
            "mean_p": (sum(r["probability"] for r in rs) / n) if n else None}


R = {"generated_from": {"predictions": len(P), "outcomes": len(O), "matched": len(rows)}}
tracks = collections.defaultdict(list)
for r in rows:
    tracks[r.get("kind")].append(r)
R["tracks"] = {k: block(k, v) for k, v in tracks.items()}
R["tracks_by_month"] = {k: {m: block(m, [r for r in v if r["month"] == m]) for m in sorted(set(r["month"] for r in v))}
                        for k, v in tracks.items()}

# (a) калибровка: по k_sigma и по вероятностным корзинам (калибровочная кривая)
cal = tracks.get("calibration", [])
R["calibration"] = {
    "by_k_sigma": {str(k): block(k, [r for r in cal if r.get("k_sigma") == k]) for k in sorted(set(r.get("k_sigma") for r in cal), key=lambda x: (x is None, x))},
    "reliability_curve": {},
    "by_asset_top": {},
}
bins = collections.defaultdict(list)
for r in cal:
    bins[round(r["probability"], 2)].append(r)
for pb, rs in sorted(bins.items()):
    if len(rs) >= 20:
        R["calibration"]["reliability_curve"][str(pb)] = {"n": len(rs), "observed": hit(rs)}
assets = collections.defaultdict(list)
for r in cal:
    assets[r["asset"]].append(r)
R["calibration"]["by_asset_top"] = {a: block(a, rs) for a, rs in sorted(assets.items(), key=lambda x: -len(x[1]))[:15]}
R["calibration"]["n_assets"] = len(assets)

# (b) провизорный: r² корзины, глубина, ярус, |амплитуда|, направление
prov = tracks.get("cascade_provisional", [])


def r2b(x):
    if x is None:
        return "нет"
    for lim, lab in ((0.02, "<0.02"), (0.05, "0.02–0.05"), (0.1, "0.05–0.10"), (0.2, "0.10–0.20"), (0.4, "0.20–0.40")):
        if x < lim:
            return lab
    return "≥0.40"


def depth(r):
    cp = r.get("cascade_path")
    return len(cp) if isinstance(cp, list) else None


R["provisional"] = {
    "by_r2": {},
    "by_depth": {},
    "by_tiers": {},
    "by_amp_abs": {},
    "by_direction": {d: block(d, [r for r in prov if r["direction"] == d]) for d in ("above", "below")},
    "raw_vs_shrunk": {},
    "first_hop_r2_proxy": {},
}
order = ["<0.02", "0.02–0.05", "0.05–0.10", "0.10–0.20", "0.20–0.40", "≥0.40", "нет"]
g = collections.defaultdict(list)
for r in prov:
    g[r2b(r.get("reliability_r2"))].append(r)
R["provisional"]["by_r2"] = {k: block(k, g[k]) for k in order if g.get(k)}
g = collections.defaultdict(list)
for r in prov:
    g[str(depth(r))].append(r)
R["provisional"]["by_depth"] = {k: block(k, v) for k, v in sorted(g.items())}
g = collections.defaultdict(list)
for r in prov:
    g["+".join(r.get("ярусы") or ["?"])].append(r)
R["provisional"]["by_tiers"] = {k: block(k, v) for k, v in sorted(g.items(), key=lambda x: -len(x[1])) if len(v) >= 15}
g = collections.defaultdict(list)
for r in prov:
    a = abs(r.get("amplitude_expected") or 0)
    lab = "<0.5%" if a < 0.005 else "0.5–1%" if a < 0.01 else "1–2%" if a < 0.02 else "2–5%" if a < 0.05 else "≥5%"
    g[lab].append(r)
R["provisional"]["by_amp_abs"] = {k: block(k, g[k]) for k in ("<0.5%", "0.5–1%", "1–2%", "2–5%", "≥5%") if g.get(k)}
# сырая уверенность (до сжатия П-1) — несёт ли информацию?
raw = [r for r in prov if isinstance(r.get("probability_raw"), (int, float))]
if raw:
    hi = [r for r in raw if r["probability_raw"] >= 0.55]
    mid = [r for r in raw if 0.45 <= r["probability_raw"] < 0.55]
    lo = [r for r in raw if r["probability_raw"] < 0.45]
    R["provisional"]["raw_vs_shrunk"] = {
        "brier_raw": sum((r["probability_raw"] - r["outcome"]) ** 2 for r in raw) / len(raw),
        "brier_shrunk": brier(raw),
        "raw>=0.55": block("hi", hi), "raw 0.45–0.55": block("mid", mid), "raw<0.45": block("lo", lo),
    }
# «ближнее звено»: цепочки длины 1 с ярусом A vs глубокие с C
near_a = [r for r in prov if depth(r) == 1 and (r.get("ярусы") or [""])[0] == "A"]
deep_c = [r for r in prov if (depth(r) or 0) >= 2 and "C" in (r.get("ярусы") or [])]
R["provisional"]["first_hop_r2_proxy"] = {"depth1_tierA": block("na", near_a), "depth>=2_with_C": block("dc", deep_c)}

# (d) edge_forward — по рёбрам
ef = tracks.get("edge_forward", [])
g = collections.defaultdict(list)
for r in ef:
    g[r.get("edge_key")].append(r)
R["edge_forward"] = {
    "n_edges_with_outcomes": len(g),
    "edges_n>=5": {k: block(k, v) for k, v in sorted(g.items(), key=lambda x: -len(x[1])) if len(v) >= 5},
    "by_r2": {},
}
g2 = collections.defaultdict(list)
for r in ef:
    g2[r2b(r.get("reliability_r2"))].append(r)
R["edge_forward"]["by_r2"] = {k: block(k, g2[k]) for k in order if g2.get(k)}

# money
R["money"] = block("money", tracks.get("cascade_money", []))

# пишем
(OUT / "report.json").write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")


def f(x, d=3):
    return "—" if x is None else f"{x:.{d}f}"


def tbl(d, title):
    out = [f"\n### {title}", "| срез | n | попаданий | 95% ДИ | Brier | skill vs база | средняя P |", "|---|---|---|---|---|---|---|"]
    for k, b in d.items():
        ci = b["hit_ci95"]
        out.append(f"| {k} | {b['n']} | {f(b['hit'])} | {f(ci[0], 2)}–{f(ci[1], 2)} | {f(b['brier'])} | {f(b['skill_vs_clim'])} | {f(b['mean_p'])} |")
    return "\n".join(out)


md = [f"# Разбор треков 15.07→31.10.2026 (сгенерировано analyze_tracks.py; журналы только чтение)",
      f"\nПечатей {len(P)}, исходов {len(O)}, сматчено {len(rows)}.",
      "\n«skill vs база» = Brier skill score против постоянного прогноза «средняя частота исходов этого среза»: "
      "> 0 — вероятности несут информацию сверх базовой частоты; ≤ 0 — не несут (монетка/климатология лучше).",
      tbl(R["tracks"], "Все треки"),
      ]
for k, v in R["tracks_by_month"].items():
    md.append(tbl(v, f"{k} по месяцам печати"))
md.append(tbl(R["calibration"]["by_k_sigma"], "(a) Калибровка по k_sigma (порог = ±k·σ от якоря)"))
md.append("\n### (a) Калибровочная кривая (заявлено → наблюдено)\n| заявлено P | n | наблюдено |\n|---|---|---|")
for pb, v in R["calibration"]["reliability_curve"].items():
    md.append(f"| {pb} | {v['n']} | {f(v['observed'])} |")
md.append(tbl(R["calibration"]["by_asset_top"], f"(a) Калибровка по активам (топ-15 из {R['calibration']['n_assets']})"))
md.append(tbl(R["provisional"]["by_r2"], "(b) Провизорный: по reliability_r2 (сила связи в истории)"))
md.append(tbl(R["provisional"]["by_depth"], "(b) Провизорный: по глубине цепочки (звеньев)"))
md.append(tbl(R["provisional"]["by_tiers"], "(b) Провизорный: по ярусам цепочки (n≥15)"))
md.append(tbl(R["provisional"]["by_amp_abs"], "(b) Провизорный: по |ожидаемая амплитуда|"))
md.append(tbl(R["provisional"]["by_direction"], "(b) Провизорный: по направлению"))
md.append(tbl(R["provisional"]["first_hop_r2_proxy"], "(b) Гипотеза 15.07: ближнее звено яруса A vs глубокие с C"))
if R["provisional"]["raw_vs_shrunk"]:
    rv = R["provisional"]["raw_vs_shrunk"]
    md.append(f"\n### (b) Сырая уверенность картографа/движка vs сжатая П-1\nBrier сырой {f(rv['brier_raw'])} · Brier сжатой {f(rv['brier_shrunk'])}")
    md.append(tbl({k: rv[k] for k in ("raw>=0.55", "raw 0.45–0.55", "raw<0.45")}, "по корзинам сырой P"))
md.append(tbl(R["edge_forward"]["by_r2"], "(d) edge_forward по r²"))
md.append(tbl(R["edge_forward"]["edges_n>=5"], f"(d) edge_forward по рёбрам (n≥5; всего рёбер с исходами {R['edge_forward']['n_edges_with_outcomes']})"))
md.append(tbl({"money": R["money"]}, "Денежный трек (все исходы — июнь, до fail-closed суда)"))
(OUT / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
print("\n".join(md))
