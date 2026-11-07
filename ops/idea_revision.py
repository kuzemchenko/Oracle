# -*- coding: utf-8 -*-
"""ops/idea_revision.py — ПЕРЕСМОТР ИДЕИ БЕЗ ПЕРЕПИСЫВАНИЯ ИСТОРИИ (аудит 07.11, находка №6).

Правило: исходная карточка и её прогноз неизменны. Всё, что меняется после записи в журнал, — это
ОТДЕЛЬНАЯ версия со своей датой, причиной и ссылкой на исходник:

  fact_correction  — исправлен факт; условия ставки (цель/стоп/порог/срок/вход) менять НЕЛЬЗЯ;
  new_hypothesis   — новая версия гипотезы (другой механизм, другой срок эффекта);
  new_trade_terms  — новое торговое условие (вход/цель/стоп/срок).

Версия пишется в reports/ideas/revisions/<stem>.rNN.json, исходник не трогается. Итоги исходной
карточки считает mathlib.idea_outcome по ИСХОДНЫМ условиям; итог версии — отдельно, от её даты.

  python3 ops/idea_revision.py verify                → исходные карточки не правились после записи
  python3 ops/idea_revision.py new <stem> <kind> "<причина>" '<json changes>' [--date YYYY-MM-DD]
  python3 ops/idea_revision.py evaluate <stem> <closes.json> → три итога исходника и версий
"""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IDEAS = ROOT / "reports" / "ideas"
REV_DIR = IDEAS / "revisions"
PINS = REV_DIR / "originals.json"         # отпечатки исходников, записанных до аудита 07.11
KINDS = ("fact_correction", "new_hypothesis", "new_trade_terms")
BET_FIELDS = ("target_price", "stop_price", "entry_limit", "spec9.threshold", "spec9.resolve_by",
              "spec9.direction", "direction", "spec9.asset", "ticker")


def _sha(card):
    try:
        from ops import hunter_seal as HS
    except ImportError:
        import hunter_seal as HS
    return HS.card_sha256(card)


def _load(p):
    return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))


def _get(card, path):
    cur = card
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
    return cur


def _sealed(pred_path=None):
    """{idea_file → запись печати hunter_idea}."""
    p = pathlib.Path(pred_path or ROOT / "journal" / "predictions.jsonl")
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") == "hunter_idea" and r.get("idea_file"):
                out.setdefault(str(r["idea_file"]), r)
    return out


def _pins(path=None):
    p = pathlib.Path(path or PINS)
    return _load(p) if p.exists() else {}


def original_hash(stem, pred_path=None, pins_path=None):
    """Эталонный отпечаток исходника: из записи печати (новые идеи) или из закреплённого списка."""
    name = pathlib.Path(stem).name
    rec = _sealed(pred_path).get(name + ".push.txt")
    if rec and rec.get("карточка_sha256"):
        return rec["карточка_sha256"], "запись печати"
    pin = _pins(pins_path).get(name)
    if pin:
        return pin["sha256"], "закреплено " + str(pin.get("pinned_at"))
    return None, None


def verify(ideas_dir=None, pred_path=None, pins_path=None):
    """→ список нарушений: исходная карточка записанной идеи отличается от эталона."""
    bad = []
    for name, rec in sorted(_sealed(pred_path).items()):
        stem = pathlib.Path(ideas_dir or IDEAS) / name.replace(".push.txt", "")
        card_p = pathlib.Path(str(stem) + ".card.json")
        if not card_p.exists():
            continue
        ref, src = original_hash(stem, pred_path, pins_path)
        if ref is None:
            bad.append(f"{card_p.name}: нет эталонного отпечатка (ни в печати, ни в {PINS.name}) — неизменность не проверить")
            continue
        if _sha(_load(card_p)) != ref:
            bad.append(f"{card_p.name}: исходная карточка изменена после записи прогноза (эталон — {src}); "
                       "правка — только отдельной версией через ops/idea_revision.py")
    return bad


def new_revision(stem, kind, reason, changes, date=None, out_dir=None, pred_path=None, pins_path=None):
    """Создать версию. changes: {путь_поля: новое_значение}. → путь файла версии (ValueError при нарушении)."""
    if kind not in KINDS:
        raise ValueError(f"вид пересмотра {kind!r} не из {KINDS}")
    if not str(reason or "").strip():
        raise ValueError("пересмотр без причины")
    stem = pathlib.Path(stem)
    card = _load(str(stem) + ".card.json")
    ref, _src = original_hash(stem, pred_path, pins_path)
    cur = _sha(card)
    if ref and ref != cur:
        raise ValueError("исходная карточка уже изменена после записи — сначала восстанови исходник из git")
    touched = [k for k in changes if k in BET_FIELDS]
    if kind == "fact_correction" and touched:
        raise ValueError("исправление факта не может менять условия ставки: " + ", ".join(touched)
                         + " — это new_hypothesis или new_trade_terms")
    date = date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    d = pathlib.Path(out_dir or REV_DIR)
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob(stem.name + ".r*.json"))) + 1
    rec = {"revision_of": stem.name, "revision_no": n, "date": date, "kind": kind, "reason": reason,
           "original_card_sha256": ref or cur,
           "changes": {k: {"old": _get(card, k), "new": v} for k, v in changes.items()},
           "rule": ("исходный прогноз и его условия не меняются и сверяются как записаны; итог этой версии "
                    "считается отдельно, от её даты, и не улучшает счёт исходной идеи")}
    out = d / f"{stem.name}.r{n:02d}.json"
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out


def apply_changes(card, changes):
    c = json.loads(json.dumps(card))
    for path, v in changes.items():
        cur = c
        keys = path.split(".")
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = v["new"] if isinstance(v, dict) and "new" in v else v
    return c


def evaluate_all(stem, closes, entry_from=None):
    """Три итога исходника + каждой версии отдельно (mathlib.idea_outcome)."""
    from mathlib import idea_outcome as IO
    try:
        from ops import hunter_seal as HS
    except ImportError:
        import hunter_seal as HS
    stem = pathlib.Path(stem)
    card = _load(str(stem) + ".card.json")
    start = entry_from or str(HS.hunt_date_from_name(stem.name))
    out = {"исходная": IO.evaluate(card, closes, start)}
    for p in sorted(REV_DIR.glob(stem.name + ".r*.json")):
        r = _load(p)
        out[f"версия r{r['revision_no']:02d} от {r['date']} ({r['kind']})"] = IO.evaluate(
            apply_changes(card, r["changes"]), closes, r["date"])
    return out


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] == ["verify"]:
        bad = verify()
        print("\n".join(bad) if bad else "исходные карточки записанных идей не менялись")
        return 1 if bad else 0
    if a[:1] == ["new"] and len(a) >= 5:
        date = a[a.index("--date") + 1] if "--date" in a else None
        print(new_revision(a[1], a[2], a[3], json.loads(a[4]), date=date))
        return 0
    if a[:1] == ["evaluate"] and len(a) >= 3:
        print(json.dumps(evaluate_all(a[1], _load(a[2])), ensure_ascii=False, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
