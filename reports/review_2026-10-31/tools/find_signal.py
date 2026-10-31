# -*- coding: utf-8 -*-
"""«Где сигнал»: есть ли среди провизорных исходов подмножество с попаданием выше базы — честно, с поправкой
на множественность (Бенджамини–Хохберг, mathlib.fdr, как в скане §6). Журналы/протоколы — только чтение.

Признаки идеи берём из протокола прогона (граф_отбор.провизорный_трек / money_трек по run_id+актив):
цепочка, порядок звена, чокпоинт, лаг, источник карты (авторская/картограф), инверсия, якорь, тип события;
из печати: ярусы, глубина, r², знак, |амплитуда|, месяц, тикер. Срез = (признак, значение), n ≥ MIN_N.
Тест: двусторонний биномиальный против базы трека. Печатаем все срезы с n≥MIN_N, отмечаем прошедшие FDR q=0.10.
"""
import collections
import glob
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mathlib.fdr import benjamini_hochberg  # noqa: E402

MIN_N = 40
P = [json.loads(l) for l in open(ROOT / "journal/predictions.jsonl", encoding="utf-8") if l.strip()]
O = {o["hash"]: int(o["outcome"]) for o in (json.loads(l) for l in open(ROOT / "journal/outcomes.jsonl", encoding="utf-8") if l.strip())}

# признаки из протоколов
feat = {}
for f in sorted(glob.glob(str(ROOT / "journal/funnel_logs/ef_2026*.json"))):
    d = json.load(open(f, encoding="utf-8"))
    g = d.get("граф_отбор") or {}
    run = d.get("run_id")
    for trk in ("провизорный_трек", "money_трек"):
        for e in g.get(trk) or []:
            ev = str(e.get("событие") or "")
            feat[(run, e.get("актив"))] = {
                "цепочка": e.get("цепочка"), "порядок": e.get("порядок"), "чокпоинт": e.get("чокпоинт"),
                "лаг": e.get("лаг_дней"), "карта": e.get("источник_карты"), "инверсия": e.get("инверсия"),
                "якорь": e.get("якорь"), "тип_события": "ценовой сигнал" if ev.startswith("ценовой") else ("новость" if ev else "?"),
                "трек_в_протоколе": trk,
            }

rows = []
for p in P:
    if p.get("kind") != "cascade_provisional" or p["hash"] not in O:
        continue
    r = {"outcome": O[p["hash"]], "asset": p["asset"], "месяц": (p.get("sealed_at") or "")[:7],
         "знак": p["direction"], "ярусы": "+".join(p.get("ярусы") or ["?"]),
         "глубина": len(p["cascade_path"]) if isinstance(p.get("cascade_path"), list) else None,
         "r2": p.get("reliability_r2"), "амп": abs(p.get("amplitude_expected") or 0),
         "день_недели": None}
    try:
        import datetime
        r["день_недели"] = datetime.date.fromisoformat(p["sealed_at"][:10]).strftime("%a")
    except Exception:  # noqa: BLE001
        pass
    r.update(feat.get((p.get("run_id"), p["asset"]), {}))
    cp = p.get("cascade_path") or []
    r["исток"] = cp[0].get("from") if cp else None
    r["лаг_ребра"] = cp[0].get("lag") if cp else None
    rows.append(r)

base = sum(r["outcome"] for r in rows) / len(rows)


def binom_two_sided(k, n, p):
    """Двусторонний биномиальный p-value (точный, суммой хвостов; без scipy)."""
    def pmf(i):   # в лог-пространстве: при n>1000 comb переполняет float
        return math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                        + i * math.log(p) + (n - i) * math.log(1 - p))
    obs = pmf(k)
    return min(1.0, sum(pmf(i) for i in range(n + 1) if pmf(i) <= obs + 1e-15))


def r2b(x):
    if x is None:
        return "нет"
    return "<0.05" if x < 0.05 else "0.05–0.2" if x < 0.2 else "≥0.2"


def ampb(a):
    return "<1%" if a < 0.01 else "1–3%" if a < 0.03 else "≥3%"


def lagb(x):
    if x is None:
        return "нет"
    return "0" if x == 0 else "1–5" if x <= 5 else "6–20" if x <= 20 else ">20"


FEATURES = {
    "тип_события": lambda r: r.get("тип_события"), "карта": lambda r: r.get("карта"),
    "цепочка": lambda r: r.get("цепочка"), "порядок": lambda r: r.get("порядок"),
    "чокпоинт": lambda r: r.get("чокпоинт"), "инверсия": lambda r: r.get("инверсия"),
    "лаг(узел)": lambda r: lagb(r.get("лаг")), "лаг(ребро)": lambda r: lagb(r.get("лаг_ребра")),
    "якорь": lambda r: r.get("якорь"), "исток_ребра": lambda r: r.get("исток"),
    "тикер": lambda r: r["asset"], "месяц": lambda r: r["месяц"], "знак": lambda r: r["знак"],
    "ярусы": lambda r: r["ярусы"], "глубина": lambda r: r.get("глубина"), "r2": lambda r: r2b(r.get("r2")),
    "|амплитуда|": lambda r: ampb(r["амп"]), "день_недели": lambda r: r.get("день_недели"),
    "трек_в_протоколе": lambda r: r.get("трек_в_протоколе"),
}
tests = []
for name, fn in FEATURES.items():
    g = collections.defaultdict(list)
    for r in rows:
        g[fn(r)].append(r["outcome"])
    for val, ys in g.items():
        n = len(ys)
        if n < MIN_N:
            continue
        k = sum(ys)
        tests.append({"признак": name, "значение": str(val), "n": n, "hit": k / n,
                      "p": binom_two_sided(k, n, base)})
# парные срезы (2 признака) — только крупные
PAIRS = [("карта", "порядок"), ("тип_события", "знак"), ("карта", "знак"), ("чокпоинт", "знак"),
         ("карта", "r2"), ("тип_события", "лаг(узел)"), ("ярусы", "знак"), ("порядок", "знак")]
for a, b in PAIRS:
    g = collections.defaultdict(list)
    for r in rows:
        g[(str(FEATURES[a](r)), str(FEATURES[b](r)))].append(r["outcome"])
    for val, ys in g.items():
        n = len(ys)
        if n < MIN_N:
            continue
        k = sum(ys)
        tests.append({"признак": f"{a}×{b}", "значение": "/".join(val), "n": n, "hit": k / n,
                      "p": binom_two_sided(k, n, base)})

pv = [t["p"] for t in tests]
bh = benjamini_hochberg(pv, q=0.10)
# совместимость: функция может вернуть маску или индексы/порог
if isinstance(bh, dict):
    rej = bh.get("rejected") or []
    passed = {i for i, b in enumerate(rej) if b} if (rej and isinstance(rej[0], bool)) else set(rej)
elif isinstance(bh, (list, tuple)) and bh and isinstance(bh[0], bool):
    passed = {i for i, b in enumerate(bh) if b}
else:
    passed = set(bh) if isinstance(bh, (list, tuple, set)) else set()
for i, t in enumerate(tests):
    t["FDR_q0.10"] = i in passed
tests.sort(key=lambda t: t["p"])

(OUT / "signal.json").write_text(json.dumps({"base": base, "n": len(rows), "tests": tests}, ensure_ascii=False, indent=1), encoding="utf-8")
md = [f"# Где сигнал? Провизорный трек, {len(rows)} исходов, база {base:.3f} (find_signal.py)",
      f"\nСрезов проверено: {len(tests)} (n ≥ {MIN_N}); прошли FDR q=0.10: **{len(passed)}**. "
      "p — двусторонний биномиальный тест против базы трека; без поправки ~10% срезов «значимы» случайно.",
      "\n| признак | значение | n | попаданий | p | FDR |", "|---|---|---|---|---|---|"]
for t in tests[:40]:
    md.append(f"| {t['признак']} | {t['значение']} | {t['n']} | {t['hit']:.3f} | {t['p']:.4f} | {'✅' if t['FDR_q0.10'] else ''} |")
md.append(f"\n…всего строк {len(tests)}; полный список — signal.json.")
(OUT / "SIGNAL.md").write_text("\n".join(md) + "\n", encoding="utf-8")
print("\n".join(md))
