# -*- coding: utf-8 -*-
"""ops/point_b.py — замер «точки Б» дорожной карты v3 (V7; аудит 03.11 P0). ТОЛЬКО ЧИТАЕТ журналы.

Точка Б1 — 28.11.2026, точка Б — 01.02.2027 (reports/review_2026-10-31/ДОРОЖНАЯ_КАРТА_v3.md, §1).
Каждый критерий считается кодом из журналов (П16: из журналов, не из пересказа):
  • дни с идеей — journal/deliveries.jsonl (идея охотника или честный ноль до 15:00 UTC); до появления
    журнала доставок (04.11) — по печатям hunter_idea (печать идёт перед доставкой);
  • прокачка — стадии day2 в journal/deep_dive.jsonl;
  • событийный трек — печати и исходы kind=thesis_event; идеи охотника — kind=hunter_idea;
  • умение — mathlib/skill (Brier против БАЗЫ шума, кластер = неделя сверки × механизм; N<40 —
    «данных мало, вердикт отложен»);
  • связи новых классов — knowledge/edge_candidates.jsonl по полю origin;
  • пятничные отчёты — reports/machinist/friday_*.md и доставки «пятничный разбор»;
  • этапы — ops/roadmap_state.yaml.
Выход: reports/point_b/<точка>_<дата>.{md,json}; без --write — печать на экран.
Что считается «днём с идеей» и прочие определения экзамена подписывает владелец
(journal/proposals/point_b_definitions_2026-11.md) — до подписи это рабочие определения кода.
"""
import argparse
import datetime
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

J = ROOT / "journal"
PRED = J / ("predictions" + ".jsonl")
OUTC = J / ("outcomes" + ".jsonl")
POINTS = {"Б1": "2026-11-28", "Б": "2027-02-01"}
# поправка владельца 04.11 (journal/proposals/signature_20261104_point_b_amendment.md): идея «для обсуждения»
# (показана, но в счёт не записана — не дотянула до правил записи) тоже закрывает запрос «найди идею»
IDEA_KINDS = ("идея охотника", "честный ноль", "идея для обсуждения")


def _jsonl(path):
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def idea_days(deliveries, preds, since="2026-11-02", until=None):
    """Будние дни с идеей охотника или честным нолём, доставленными до 15:00 UTC (+ дни печатей
    hunter_idea до появления журнала доставок). → отсортированный список дат."""
    days = set()
    for r in deliveries:
        ts = str(r.get("ts") or "")
        if r.get("kind") in IDEA_KINDS and ts[:10] >= since and (not until or ts[:10] <= until) \
                and ts[11:13] < "15":
            days.add(ts[:10])
    first_log = min((str(r.get("ts"))[:10] for r in deliveries if r.get("ts")), default="9999")
    for r in preds:
        d = str(r.get("sealed_at") or "")[:10]
        if r.get("kind") == "hunter_idea" and since <= d < first_log and (not until or d <= until):
            days.add(d)
    return sorted(d for d in days if datetime.date.fromisoformat(d).isoweekday() <= 5)


def requests_closed(root, deliveries):
    """03.11 (охота по запросу): запрос владельца = заявка reports/ideas/claims/<дата>.txt; закрыт, если в тот
    же или следующий день ушла идея охотника или честный ноль. → (запросов, закрыто, [незакрытые даты])."""
    d = pathlib.Path(root) / "reports" / "ideas" / "claims"
    days = sorted(f.stem for f in d.glob("*.txt")) if d.is_dir() else []
    got = {str(r.get("ts"))[:10] for r in deliveries if r.get("kind") in IDEA_KINDS}
    open_ = []
    for day in days:
        try:
            nxt = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
        except ValueError:
            continue
        if day not in got and nxt not in got:
            open_.append(day)
    return len(days), len(days) - len(open_), open_


def _week(iso):
    try:
        y, w, _ = datetime.date.fromisoformat(str(iso)[:10]).isocalendar()
        return f"{y}-W{w:02d}"
    except ValueError:
        return "?"


def track_skill(kind, preds, outs):
    """Печати/исходы трека и умение против базы шума (вероятность_база из печати)."""
    from mathlib import skill as SK
    sealed = [r for r in preds if r.get("kind") == kind]
    by_hash = {r.get("hash"): r for r in sealed}
    res = [o for o in outs if o.get("kind") == kind and o.get("outcome") in (0, 1) and o.get("hash") in by_hash]
    ps, bs, ys, cl = [], [], [], []
    for o in res:
        s = by_hash[o["hash"]]
        if s.get("вероятность_база") is None or s.get("probability") is None:
            continue
        ps.append(s["probability"])
        bs.append(s["вероятность_база"])
        ys.append(o["outcome"])
        cl.append(_week(s.get("resolve_by")) + "|" + str(s.get("mechanism_id") or s.get("thesis_id") or "?"))
    sk = SK.brier_skill_vs_base(ps, bs, ys, clusters=cl)
    hits = sum(ys)
    return {"печатей": len(sealed), "исходов": len(res), "с_базой": len(ys), "сбылось": hits,
            "база_ожидание": round(sum(bs), 2) if bs else None, "умение": sk}


def measure(today=None, root=None):
    root = pathlib.Path(root or ROOT)
    today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    j = root / "journal"
    deliveries = _jsonl(j / "deliveries.jsonl")
    preds = _jsonl(j / ("predictions" + ".jsonl"))
    outs = _jsonl(j / ("outcomes" + ".jsonl"))
    dd = _jsonl(j / "deep_dive.jsonl")
    edges = _jsonl(root / "knowledge" / "edge_candidates.jsonl")
    days = idea_days(deliveries, preds, until=today)
    zero_days = sorted({str(r.get("ts"))[:10] for r in deliveries if r.get("kind") == "честный ноль"})
    passports = sorted({(r.get("run_id"), r.get("asset")) for r in dd if r.get("stage") == "day2"}, key=str)
    first_log = min((str(r.get("ts"))[:10] for r in deliveries if r.get("ts")), default="9999")
    hunter_pushes = [r for r in deliveries if r.get("kind") == "идея охотника"] + \
        [r for r in preds if r.get("kind") == "hunter_idea" and str(r.get("sealed_at") or "")[:10] < first_log]
    fridays = sorted(p.name for p in (root / "reports" / "machinist").glob("friday_*.md")) \
        if (root / "reports" / "machinist").is_dir() else []
    origins = {}
    for e in edges:
        origins[str(e.get("origin") or "?")] = origins.get(str(e.get("origin") or "?"), 0) + 1
    try:
        import yaml
        st = (yaml.safe_load((root / "ops" / "roadmap_state.yaml").read_text(encoding="utf-8")) or {}).get("этапы") or {}
    except Exception:                                   # noqa: BLE001 — без состояния карта честно «?»
        st = {}
    stages = {k: str((v or {}).get("статус")) for k, v in st.items()} if isinstance(st, dict) else {}
    thesis = track_skill("thesis_event", preds, outs)
    hunter = track_skill("hunter_idea", preds, outs)
    alive = sorted({str(r.get("ts"))[:10] for r in deliveries if r.get("kind") == "бот жив"})
    n_req, n_closed, req_open = requests_closed(root, deliveries)
    crit = [
        # 03.11: охота по запросу владельца — главный счёт потока: закрытые запросы (определение — на подпись)
        ("Б1", "твои запросы «найди идею», закрытые идеей (в счёт или для обсуждения) или честным «нечего»", n_closed, 10),
        ("Б1", "дни с идеей охотника или честным нолём до 15:00 UTC (старое определение)", len(days), 15),
        ("Б1", "полный цикл прокачки «Качаем 2 дня» дошёл до паспорта", len(passports), 1),
        ("Б1", "этапы V0–V2 закрыты гейтами", sum(1 for k in ("V0", "V1", "V2") if stages.get(k) == "done"), 3),
        ("Б1", "событийный трек: запечатано вопросов", thesis["печатей"], 1),
        ("Б", "идеи охотника доставлены (формат одобрен владельцем)", len(hunter_pushes), 10),
        ("Б", "идеи прошли прокачку до паспорта", len(passports), 4),
        ("Б", "событийный трек: запечатано вопросов 3–13 недель", thesis["печатей"], 60),
        ("Б", "событийный трек: сверено исходов", thesis["исходов"], 40),
        ("Б", "связи новых классов под наблюдением (с происхождением)", sum(origins.values()), 60),
        ("Б", "пятничные отчёты машиниста", len(fridays), 13),
    ]
    return {"дата": today, "точки": POINTS, "критерии": [
        {"точка": p, "что": what, "сейчас": now, "цель": goal, "выполнено": now >= goal} for p, what, now, goal in crit],
        "запросы": {"всего": n_req, "закрыто": n_closed, "незакрытые": req_open},
        "дни_с_идеей": days, "дни_честного_ноля": zero_days, "паспорта": [list(x) for x in passports], "этапы": stages,
        "событийный_трек": thesis, "идеи_охотника": hunter, "происхождение_связей": origins,
        "пятницы": fridays, "дни_бот_жив": alive[-10:],
        "код_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()}


def _skill_words(t):
    sk = t["умение"]
    if t["исходов"] == 0:
        return "исходов ещё нет"
    base = f"сбылось {t['сбылось']} из {t['с_базой']}"
    if t["база_ожидание"] is not None:
        base += f", от обычной качки цен сбылось бы около {t['база_ожидание']}"
    if sk["verdict"] == "данных мало":
        return base + "; данных мало — вердикт об умении отложен"
    return base + f"; вердикт: {sk['verdict']}"


def render_md(m):
    L = [f"# Замер точки Б — {m['дата']}", "",
         "Что это значит для продукта: таблица показывает, сколько из обещанного к 28 ноября (Б1) и к "
         "1 февраля (Б) уже есть в журналах системы. Считает код, только чтение; определения экзамена "
         "подписывает владелец.", "",
         "| Точка | Критерий | Сейчас | Цель | Есть |", "|---|---|---|---|---|"]
    for c in m["критерии"]:
        L.append(f"| {c['точка']} | {c['что']} | {c['сейчас']} | {c['цель']} | {'да' if c['выполнено'] else 'нет'} |")
    L += ["", "## Умение предсказывать (против базы шума)",
          f"- Идеи охотника: печатей {m['идеи_охотника']['печатей']}; {_skill_words(m['идеи_охотника'])}.",
          f"- Событийный трек: печатей {m['событийный_трек']['печатей']}; {_skill_words(m['событийный_трек'])}.",
          "", "## Детали",
          f"- Дни с идеей: {', '.join(m['дни_с_идеей']) or 'нет'}",
          f"- Этапы: {', '.join(f'{k} — {v}' for k, v in m['этапы'].items()) or 'нет данных'}",
          f"- Происхождение связей: {', '.join(f'{k}: {v}' for k, v in m['происхождение_связей'].items()) or 'нет'}",
          f"- Бот жив (последние дни): {', '.join(m['дни_бот_жив']) or 'журнал доставок пуст'}",
          f"- Хэш кода замера: {m['код_sha256'][:16]}…", "",
          "Это исследовательский инструмент, не инвестиционная рекомендация."]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="замер точки Б (только чтение журналов)")
    ap.add_argument("--today")
    ap.add_argument("--write", action="store_true", help="записать reports/point_b/<дата>.{md,json}")
    a = ap.parse_args(argv)
    m = measure(a.today)
    md = render_md(m)
    if a.write:
        d = ROOT / "reports" / "point_b"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"point_b_{m['дата']}.md").write_text(md, encoding="utf-8")
        (d / f"point_b_{m['дата']}.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
