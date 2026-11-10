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
  python3 ops/idea_revision.py status [<stem>]         → актуальный статус идеи (VQ-4), с перепроверкой
  python3 ops/idea_revision.py set-status <stem> <статус> "<основание>" [--q "<вопрос>"] [--source …]

АКТУАЛЬНЫЙ СТАТУС (VQ-4, задание владельца 09.11). История прогноза (карточка, печать, версии) — неизменна;
ТЕКУЩЕЕ РЕШЕНИЕ по идее хранится отдельно: reports/ideas/revisions/<stem>.status.json — статус, дата,
основания, оставшиеся вопросы и вся история смен. Telegram, паспорт прокачки и агент читают ОДНУ функцию
current(). Перед выдачей плана код перепроверяет: свежесть цены, срабатывание стопа, достижение цели, срок,
наступивший катализатор (нужен его результат, а не повтор ожидания). План входа печатается как действующий
ТОЛЬКО при статусе «действует»; иначе уровни — с пометкой «архив, не действует».
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


# ── актуальный статус идеи (VQ-4) ───────────────────────────────────────────────────
ACTIVE = "действует"
STATUSES = ("действует", "ждёт входа", "нужны данные", "проверить результат катализатора", "устарела",
            "цель достигнута", "срок истёк", "отклонена", "отменена условием")
# от более тяжёлого к лёгкому: итоговый статус — самый тяжёлый из сохранённого и найденного перепроверкой
SEVERITY = ("отменена условием", "отклонена", "срок истёк", "цель достигнута",
            "проверить результат катализатора", "нужны данные", "устарела", "ждёт входа", "действует")
FINAL = {"отменена условием", "отклонена", "срок истёк", "цель достигнута"}
STALE_PRICE_DAYS = 5          # последнее закрытие старше 5 календарных дней — данные устарели
# испытатель 10.11: FSLR был «действует» с «вход можно делать сейчас», хотя закрытие $201.14 на 5.9% выше
# границы входа $190 — план не исполним. Тот же порог, что у сервера при записи (hunter_seal.ENTRY_GAP).
ENTRY_GAP = 0.05


def status_path(stem, out_dir=None):
    return pathlib.Path(out_dir or REV_DIR) / f"{pathlib.Path(stem).name}.status.json"


def _status_doc(stem, out_dir=None):
    p = status_path(stem, out_dir)
    try:
        return _load(p)
    except (OSError, ValueError):
        return {"stem": pathlib.Path(stem).name, "текущий": None, "история": []}


def set_status(stem, status, reasons, questions=None, source="", date=None, out_dir=None, refs=None):
    """Записать решение по идее (не трогая карточку и прогноз). reasons — основания (с источником/датой)."""
    if status not in STATUSES:
        raise ValueError(f"статус {status!r} не из {STATUSES}")
    reasons = [str(r) for r in (reasons if isinstance(reasons, list) else [reasons]) if str(r).strip()]
    if not reasons:
        raise ValueError("смена статуса без основания")
    doc = _status_doc(stem, out_dir)
    rec = {"статус": status, "дата": date or datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
           "записано": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "основания": reasons, "вопросы": [str(q) for q in (questions or [])], "источник": source,
           "ссылки": refs or []}
    doc["текущий"] = rec
    doc.setdefault("история", []).append(rec)
    p = status_path(stem, out_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return rec


def stem_for_run_id(run_id, ideas_dir=None):
    """run_id идеи охотника «h_<дата>_<ТИКЕР>» → путь-стем карточки (без .card.json) или None."""
    rid = str(run_id or "")
    if not rid.startswith("h_"):
        return None
    d = pathlib.Path(ideas_dir or IDEAS)
    stem = "idea_" + rid[2:]
    for base in (d, d / "alt"):
        if (base / (stem + ".card.json")).exists():
            return str(base / stem)
    found = sorted(d.glob(stem + "*.card.json"))
    return str(found[0])[:-len(".card.json")] if len(found) == 1 else None


def closes_from_snapshot(symbol, path=None):
    """Закрытия бумаги из снимка сервера reports/prices/closes_latest.json → [(дата, close)]; нет — []."""
    try:
        snap = _load(path or (ROOT / "reports" / "prices" / "closes_latest.json"))
    except (OSError, ValueError):
        return []
    sym = str(symbol or "").upper()
    if "." not in sym:
        sym += ".US"
    return [(str(d), float(c)) for d, c in ((snap.get("symbols") or {}).get(sym) or [])]


def recheck(card, closes, today, start=None, history=None):
    """Перепроверка перед выдачей плана: [(статус, основание, вопрос|None)] по ценам и датам карточки."""
    today = str(today)[:10]
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    up = str(spec.get("direction") or "").lower() == "above" or str(card.get("direction") or "").lower() == "вверх"
    start = str(start or "")[:10]
    found = []
    rows = [(d, c) for d, c in (closes or []) if (not start or d > start) and d <= today]
    stop, target = card.get("stop_price"), card.get("target_price")
    for d, c in rows:
        if isinstance(stop, (int, float)) and ((up and c <= stop) or (not up and c >= stop)):
            found.append(("отменена условием", f"закрытие {d} — ${c:.2f}, {'ниже' if up else 'выше'} стопа "
                                              f"${stop:.2f} (снимок цен сервера, EODHD close)", None))
            break
    for d, c in rows:
        if isinstance(target, (int, float)) and ((up and c >= target) or (not up and c <= target)):
            found.append(("цель достигнута", f"закрытие {d} — ${c:.2f} дошло до цели ${target:.2f}", None))
            break
    rb = str(spec.get("resolve_by") or "")[:10]
    if rb and rb < today:
        found.append(("срок истёк", f"срок идеи {rb} прошёл", None))
    cat = card.get("catalyst")
    cat_date = str((cat.get("date") if isinstance(cat, dict) else cat) or "")[:10]
    if cat_date and cat_date < today:
        checked = any(h.get("источник") == "катализатор" and str(h.get("дата") or "") >= cat_date
                      for h in (history or []))
        if not checked:
            what = cat.get("what") if isinstance(cat, dict) else ""
            found.append(("проверить результат катализатора",
                          f"катализатор {cat_date} уже состоялся — нужен его результат, а не повтор ожидания",
                          f"что показал катализатор {cat_date}{(': ' + str(what)[:120]) if what else ''}?"))
    lim = card.get("entry_limit")
    if isinstance(lim, (int, float)) and lim > 0 and rows and not (rb and rb < today):
        d, c = rows[-1]
        gap = (c / lim - 1) if up else (1 - c / lim)
        if gap > ENTRY_GAP:
            found.append(("ждёт входа", f"закрытие {d} — ${c:.2f}, на {gap * 100:.1f}% {'выше' if up else 'ниже'} "
                                        f"границы входа ${lim:.2f}: по плану сейчас не входить, вход — только если "
                                        f"цена вернётся к границе", None))
    if not closes:
        found.append(("устарела", "нет цен в снимке сервера — план не перепроверен", "свежая цена"))
    else:
        last_d = closes[-1][0]
        try:
            gap = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last_d)).days
        except ValueError:
            gap = None
        if gap is not None and gap > STALE_PRICE_DAYS:
            found.append(("устарела", f"последняя цена {last_d} старше {STALE_PRICE_DAYS} дн.", "свежая цена"))
    return found


def current(stem, card=None, closes=None, today=None, out_dir=None, persist=False):
    """ЕДИНЫЙ актуальный статус идеи для всех поверхностей. → {статус, дата, основания, вопросы,
    план_действует, источник, перепроверка}. persist=True — найденное перепроверкой дописывается в историю."""
    stem = pathlib.Path(stem)
    today = str(today or datetime.datetime.now(datetime.timezone.utc).date())[:10]
    if card is None:
        card = _load(str(stem) + ".card.json") if pathlib.Path(str(stem) + ".card.json").exists() else {}
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    if closes is None:
        closes = closes_from_snapshot(spec.get("asset") or card.get("ticker"))
    try:
        from ops import hunter_seal as HS
    except ImportError:
        import hunter_seal as HS
    start = HS.hunt_date_from_name(stem.name)
    doc = _status_doc(stem, out_dir)
    stored = doc.get("текущий")
    found = recheck(card, closes, today, start=start, history=doc.get("история"))
    cands = [(stored["статус"], stored.get("основания") or [], stored.get("вопросы") or [], stored.get("источник"),
              stored.get("дата"))] if stored else [(ACTIVE, ["карточка прошла проверку при выдаче"], [], "карточка",
                                                    str(start) if start else None)]
    for st, why, q in found:
        cands.append((st, [why], [q] if q else [], "перепроверка кода", today))
    cands.sort(key=lambda x: SEVERITY.index(x[0]))
    top = cands[0]
    reasons = [r for c in cands if c[0] == top[0] for r in c[1]]
    questions = [q for c in cands for q in c[2]]
    out = {"статус": top[0], "дата": top[4], "основания": reasons, "вопросы": questions, "источник": top[3],
           "план_действует": top[0] == ACTIVE, "перепроверка": [f[0] for f in found],
           "последняя_цена": closes[-1] if closes else None}
    if persist and (not stored or stored["статус"] != top[0]) and top[3] == "перепроверка кода":
        set_status(stem, top[0], reasons, questions, source="перепроверка кода", date=today, out_dir=out_dir)
    return out


def _usd(x):
    try:
        return f"${float(x):.2f}"
    except (TypeError, ValueError):
        return None


def plan_or_archive(card, cur, plan_lines):
    """План входа — только при статусе «действует». Иначе — архивные уровни с явной пометкой."""
    if cur.get("план_действует"):
        return list(plan_lines)
    if cur.get("статус") == "ждёт входа":
        # уровни не архивные — план жив, но исполним только при возврате цены к границе входа
        why = "; ".join(cur.get("основания") or [])[:300]
        return [f"Сейчас по плану не входить — {why}."] + list(plan_lines)
    lv = ", ".join(x for x in (
        ("вход " + _usd(card.get("entry_limit"))) if _usd(card.get("entry_limit")) else None,
        ("цель " + _usd(card.get("target_price"))) if _usd(card.get("target_price")) else None,
        ("стоп " + _usd(card.get("stop_price"))) if _usd(card.get("stop_price")) else None) if x)
    why = "; ".join(cur.get("основания") or [])[:300]
    out = [f"План входа НЕ действует — статус идеи «{cur['статус']}»: {why}."]
    if lv:
        out.append(f"Архивные уровни из исходной карточки (только для истории, не для действий): {lv}.")
    if cur.get("вопросы"):
        out.append("Открытые вопросы: " + "; ".join(cur["вопросы"])[:300] + ".")
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
    if a[:1] == ["status"]:
        stems = [a[1]] if len(a) > 1 else [str(p)[:-len(".card.json")] for p in sorted(IDEAS.glob("idea_*.card.json"))]
        for st in stems:
            cur = current(st)
            print(pathlib.Path(st).name, "→", cur["статус"], "|", "; ".join(cur["основания"])[:200],
                  "| вопросы:", "; ".join(cur["вопросы"])[:160] or "нет")
        return 0
    if a[:1] == ["set-status"] and len(a) >= 4:
        qs = [a[i + 1] for i, x in enumerate(a) if x == "--q" and i + 1 < len(a)]
        src = a[a.index("--source") + 1] if "--source" in a else "владелец/сессия"
        print(set_status(a[1], a[2], [a[3]], qs, source=src))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
