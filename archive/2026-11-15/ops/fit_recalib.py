# -*- coding: utf-8 -*-
"""ops/fit_recalib.py — V4 (карта v3): фит изотонической рекалибровки на боевом журнале.

Читает journal/predictions.jsonl + journal/outcomes.jsonl (ТОЛЬКО чтение, append-only
журналы не трогаются), джойнит по hash, берёт сырые вероятности probability_raw
(П-1 09.07: сырая пишется рядом с official-константой), фитит PAV walk-forward
с кластерным бутстрэпом по дням (mathlib/calibration/recalib.py) по каждому треку
с достаточной выборкой и пишет ПРЕДЛОЖЕНИЕ в knowledge/prob_recalib_proposal.yaml.

Гейт V4 (ops/roadmap_state.yaml): спред калиброванных ≥ 20 п.п. И бины монотонны
(монотонность у изотоники по построению). Вердикт по гейту печатается честно —
«сигнала нет» тоже легитимный результат (аудит 31.10 это предсказывает).

Применение к боевому seal — ОТДЕЛЬНЫЙ шаг со stage-review; этот скрипт ничего
в боевом контуре не меняет.
"""
import collections
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml                                           # noqa: E402

from mathlib.calibration import recalib as R          # noqa: E402

PRED = ROOT / "journal" / "predictions.jsonl"
OUTC = ROOT / "journal" / "outcomes.jsonl"
DEST = ROOT / "knowledge" / "prob_recalib_proposal.yaml"
MIN_N = 100
GATE_SPREAD_PP = 20.0


def _load(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _track_of(p):
    t = p.get("track")
    if t:
        return t
    return p.get("kind") or p.get("tag") or "?"


def main():
    preds = _load(PRED)
    out_by_hash = {o.get("hash"): o for o in _load(OUTC)
                   if o.get("hash") and o.get("outcome") in (0, 1)}
    by_track = collections.defaultdict(list)
    raw_total = 0
    for p in preds:
        pr = p.get("probability_raw")
        if pr is None and p.get("kind") in ("thesis_event",):
            # аудит 01.11: трек V5 печатает сырую вероятность экономиста ПРЯМО в probability
            # (shrink П-1 его не трогает) — для него probability и есть raw
            pr = p.get("probability")
        if pr is None:
            continue
        raw_total += 1
        o = out_by_hash.get(p.get("hash"))
        if not o:
            continue
        by_track[_track_of(p)].append({
            "p_raw": float(pr), "outcome": int(o["outcome"]),
            "sealed_at": p.get("sealed_at") or "",
        })
    print(f"печатей с probability_raw: {raw_total}; из них с исходом: "
          f"{sum(len(v) for v in by_track.values())}; треки: "
          f"{ {k: len(v) for k, v in sorted(by_track.items())} }")

    doc = {
        "версия": 1,
        "дата_фита": datetime.date.today().isoformat(),
        "гейт_V4": f"спред калиброванных ≥ {GATE_SPREAD_PP:.0f} п.п. И бины монотонны "
                   f"(монотонность — по построению изотоники)",
        "треки": {},
    }
    verdicts = []
    for track, rows in sorted(by_track.items()):
        if len(rows) < MIN_N:
            doc["треки"][track] = {"ОТКАЗ": f"мало исходов: {len(rows)} < {MIN_N}"}
            continue
        rep = R.fit_walk_forward(rows)
        if "ОТКАЗ" in rep:
            doc["треки"][track] = rep
            continue
        entry = R.report_to_yaml_dict(
            rep, track=track,
            source_note="journal/predictions.jsonl(probability_raw) × journal/outcomes.jsonl, "
                        "join по hash, фит до 2026-11-01")
        spread = rep["спред_калиброванных_пп"]
        bt = rep.get("бутстрэп_по_дням") or {}
        gate_ok = spread >= GATE_SPREAD_PP
        signif = bool(bt.get("преимущество_значимо"))
        entry["вердикт_гейта"] = {
            "спред_пп": spread,
            "спред_достаточен": gate_ok,
            "brier_преимущество_значимо_по_дням": signif,
            "итог": ("ГЕЙТ ПРОЙДЕН" if (gate_ok and signif) else
                     "ГЕЙТ НЕ ПРОЙДЕН — рекалибровка различает идеи слабо; "
                     "это согласуется с аудитом 31.10 (skill≈0), не поломка"),
        }
        doc["треки"][track] = entry
        verdicts.append((track, len(rows), spread, signif, rep["OOS"]))

    DEST.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
                    encoding="utf-8")
    print(f"→ {DEST.relative_to(ROOT)}")
    for track, n, spread, signif, oos in verdicts:
        print(f"  {track}: n={n}, спред={spread} пп, значимо={signif}, OOS={oos}")


if __name__ == "__main__":
    main()
