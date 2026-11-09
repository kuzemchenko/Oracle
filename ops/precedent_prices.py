# -*- coding: utf-8 -*-
"""ops/precedent_prices.py — ценовая проверка прецедентов библиотеки механизмов (V6, Q-прецеденты 09.11).

  python3 ops/precedent_prices.py [--ann knowledge/precedent_annotations.json] [--write]

Разметка (бумага, даты, заявленный ход дословной цитатой) — knowledge/precedent_annotations.json; цены —
ops/history_prices (Yahoo/FRED, кэш .cache/history); расчёт — mathlib/precedent_check. Итог:
knowledge/precedent_checks.json (вердикт на каждый прецедент — его читают orchestrator/mechanisms.py и
охотник через analogs) + отчёт reports/mechanisms/precedent_prices_<дата>.md. Журналы не трогает.
"""
import argparse
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
ANN = ROOT / "knowledge" / "precedent_annotations.json"
OUT = ROOT / "knowledge" / "precedent_checks.json"


def run(ann_path=None, series_fn=None, mechs=None):
    import yaml
    from mathlib import precedent_check as PC
    if series_fn is None:
        import history_prices as HP
        series_fn = HP.series
    if mechs is None:
        mechs = yaml.safe_load((ROOT / "knowledge" / "mechanisms.yaml").read_text(encoding="utf-8"))["механизмы"]
    by = {(m["id"], i): p for m in mechs for i, p in enumerate(m.get("precedents") or [])}
    anns = json.loads(pathlib.Path(ann_path or ANN).read_text(encoding="utf-8"))
    cache, results = {}, []
    for a in anns:
        p = by.get((a.get("mech"), int(a.get("idx", -1))))
        if p is None:
            results.append({"mech": a.get("mech"), "idx": a.get("idx"), "вердикт": "разметка отвергнута",
                            "причина": "нет такого прецедента в библиотеке"})
            continue
        sym = a.get("asset")
        if sym and sym not in cache:
            try:
                cache[sym] = series_fn(sym)
            except Exception:                                    # noqa: BLE001 — одна бумага ≠ весь отчёт
                cache[sym] = None
        if sym and str(sym).upper().endswith("=X") and "slack_days" not in a:
            a = {**a, "slack_days": 1}     # сдвиг дат валютных баров Yahoo на сутки
        r = PC.check(a, p.get("observed"), cache.get(sym) if sym else None)
        r.update(mech=a["mech"], idx=int(a["idx"]), year=p.get("year"), event=p.get("event"),
                 note=a.get("note") or "")
        results.append(r)
    return {"дата": datetime.date.today().isoformat(), "итого": PC.summary(results), "проверено": len(results),
            "метод": "mathlib/precedent_check: закрытия Yahoo/FRED (скорр.), уровень или экстремум окна; "
                     "подтверждён = знак тот же и величина в пределах ×2", "результаты": results}


def render_md(rep):
    L = [f"# Ценовая проверка прецедентов библиотеки механизмов — {rep['дата']}", "",
         "Что это значит для продукта: у каждого исторического примера в библиотеке механизмов записан ход рынка "
         "словами модели. Код сверил его с настоящими закрытиями. Прецедент «не подтверждён» — модель ошиблась "
         "знаком; «по знаку» — направление верное, величина другая. Охотник и агент видят эту пометку рядом "
         "с примером и не опираются на неподтверждённое.", "",
         "Итого: " + ", ".join(f"{k} — {v}" for k, v in sorted(rep["итого"].items(), key=lambda x: -x[1])), "",
         "| Механизм | Год | Бумага | Заявлено | Факт | Вердикт |", "|---|---|---|---|---|---|"]
    for r in rep["результаты"]:
        L.append(f"| {r['mech']} #{r['idx']} | {r.get('year', '')} | {r.get('asset') or '—'} | "
                 f"{r.get('заявлено_проц') if r.get('заявлено_проц') is not None else '—'} | "
                 f"{r.get('факт_проц') if r.get('факт_проц') is not None else '—'} | {r['вердикт']}"
                 f"{(' — ' + str(r.get('причина'))[:80]) if r.get('причина') else ''} |")
    return "\n".join(L) + "\n"


def lookup(mech_id, idx, path=None):
    """Пометка проверки для прецедента (для analogs/агента). → dict или None."""
    try:
        rep = json.loads(pathlib.Path(path or OUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for r in rep.get("результаты") or []:
        if r.get("mech") == mech_id and int(r.get("idx", -1)) == int(idx):
            return r
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ann")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    rep = run(a.ann)
    if a.write:
        OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        d = ROOT / "reports" / "mechanisms"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"precedent_prices_{rep['дата']}.md").write_text(render_md(rep), encoding="utf-8")
    print(json.dumps(rep["итого"], ensure_ascii=False), rep["проверено"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
