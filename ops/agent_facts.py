# -*- coding: utf-8 -*-
"""ops/agent_facts.py — ФАКТЫ О САМОЙ СИСТЕМЕ для агента диалога, только кодом (аудит 07.11).

Аудит показал, что агент в Telegram отвечал о системе по памяти и смешивал механизмы:
  №7 «наши прогнозы на уровне монетки» — про СТАРЫЙ утренний конвейер, сказано про охотника,
     у которого ещё нет ни одного сверенного исхода; «55% на момент записи» у DHT — в журнале 30%;
  №8 прогон ef_20261107 «с 16 идеями» как подтверждение работы охотника — это старый конвейер
     event_first, его утренний пуш выключен; «Качаем» ждали прокачки, которая выключена флагом.

Здесь — детерминированные ответы из журналов и конфига, с механизмом, версией, периодом, N,
метрикой и базой сравнения. Агент обязан брать их отсюда (инструменты track_record, sealed_terms,
pipeline_status, event_reaction), а проверка ответа (ops/claim_check.py) ловит, если не взял.
Журналы только читаются.
"""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRED = ROOT / "journal" / "predictions.jsonl"
OUTC = ROOT / "journal" / "outcomes.jsonl"
DELIV = ROOT / "journal" / "deliveries.jsonl"
DEEP = ROOT / "journal" / "deep_dive.jsonl"
IDEAS = ROOT / "reports" / "ideas"
FUNNEL = ROOT / "journal" / "funnel_logs"
PRES = ROOT / "config" / "presentation.yaml"

# механизм → человеческое имя и версия (что именно считает этот счёт)
MECHANISMS = {
    "hunter_idea": ("охотник за идеей v3 (веб-разведка → шлагбаум → суд других семейств)", "архитектура охоты 02.11.2026"),
    "thesis_event": ("«Шансы недели» — событийные вопросы 3–13 недель (экономист потоков)", "V5, 01–03.11.2026"),
    "calibration": ("калибровочный трек (механические пороги по волатильности)", "с июня 2026"),
    "provisional": ("старый утренний конвейер event_first — провизорные идеи", "июнь–ноябрь 2026"),
    "money": ("старый конвейер — денежный трек после суда", "июнь 2026"),
    "edge_forward": ("форвард-тест рёбер библиотеки (B4)", "с 05.07.2026"),
}


def _jsonl(p):
    p = pathlib.Path(p)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _track(rec):
    try:
        from orchestrator import resolve as R
        return R.track_for_kind(rec.get("kind"))
    except Exception:                                            # noqa: BLE001
        k = str(rec.get("kind") or "")
        return rec.get("track") or ("provisional" if k.startswith("cascade_provisional") else k)


def track_record(pred_path=None, out_path=None):
    """Счёт КАЖДОГО механизма отдельно: N записано/сверено, попадания, Brier прогноза против Brier
    постоянной базы (доля попаданий того же механизма), период. Без смешения механизмов."""
    preds = _jsonl(pred_path or PRED)
    outs = {o.get("hash"): o for o in _jsonl(out_path or OUTC) if o.get("outcome") in (0, 1)}
    groups = {}
    for p in preds:
        groups.setdefault(_track(p), []).append(p)
    rows = []
    for tr, ps in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        res = [(p, outs[p["hash"]]) for p in ps if p.get("hash") in outs]
        dates = sorted(str(p.get("sealed_at") or "")[:10] for p in ps if p.get("sealed_at"))
        row = {"механизм": tr, "что_это": MECHANISMS.get(tr, (f"прочие записи ({tr})", "—"))[0],
               "версия": MECHANISMS.get(tr, (tr, "—"))[1], "записано": len(ps), "сверено": len(res),
               "период_записи": f"{dates[0]}…{dates[-1]}" if dates else None}
        if res:
            ys = [int(o["outcome"]) for _p, o in res]
            pr = [float(p.get("probability")) for p, _o in res if isinstance(p.get("probability"), (int, float))]
            base = sum(ys) / len(ys)
            row["попаданий"] = sum(ys)
            row["доля_попаданий"] = round(base, 3)
            if len(pr) == len(ys):
                bs = sum((q - y) ** 2 for q, y in zip(pr, ys)) / len(ys)
                b0 = sum((base - y) ** 2 for y in ys) / len(ys)
                row["brier"] = round(bs, 4)
                row["brier_база_константа"] = round(b0, 4)
                row["вывод"] = ("лучше постоянной базы" if bs < b0 - 0.005 else
                                "не лучше постоянной базы" if bs <= b0 + 0.005 else "хуже постоянной базы")
            row["предупреждение"] = "выборка мала (N<30) — выводов нет" if len(ys) < 30 else None
        else:
            row["вывод"] = "сверенных исходов нет — о качестве этого механизма сказать нечего"
        rows.append(row)
    return {"метрика": "Brier (ниже — лучше) против постоянной базы = доля попаданий этого же механизма",
            "правило": "качество одного механизма не переносится на другой", "механизмы": rows,
            "источник": "journal/predictions.jsonl + journal/outcomes.jsonl (join по hash), кодом"}


def sealed_terms(ticker=None, pred_path=None):
    """Что ЗАПИСАНО в журнал по идеям охотника: вероятность (и её вид), порог, срок, цель/стоп/вход, дата."""
    rows = []
    for p in _jsonl(pred_path or PRED):
        if p.get("kind") != "hunter_idea":
            continue
        if ticker and str(p.get("asset", "")).split(".")[0].upper() != str(ticker).split(".")[0].upper():
            continue
        rows.append({"актив": p.get("asset"), "записано": p.get("sealed_at"), "файл": p.get("idea_file"),
                     "вопрос": f"закрытие {'выше' if p.get('direction') == 'above' else 'ниже'} {p.get('threshold')} "
                               f"к {str(p.get('resolve_by'))[:10]}",
                     "вероятность_записанная": p.get("probability"),
                     "вид_вероятности": p.get("probability_kind") or "субъективная (LLM охотника), не калибрована",
                     "база_шума": p.get("вероятность_база"), "цель": p.get("цель"), "стоп": p.get("стоп"),
                     "вход_не_хуже": p.get("вход_не_хуже"), "hash": str(p.get("hash"))[:12]})
    return {"записи": rows, "правило": "записанное не меняется; новая оценка — это новая версия с датой и причиной",
            "источник": "journal/predictions.jsonl"}


def _flags(path=None):
    try:
        import yaml
        return yaml.safe_load(pathlib.Path(path or PRES).read_text(encoding="utf-8")) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def pipeline_status(today=None, pred_path=None, deliv_path=None, deep_path=None, ideas_dir=None,
                    funnel_dir=None, flags=None):
    """Цепочка «запрос → охота → проверка → запись → доставка → прокачка» по фактам из файлов.
    Различает: охота без идеи (честный ноль), незавершённую охоту, брак проверки, сорванную доставку,
    ожидание выключенной функции. Старый утренний конвейер показан ОТДЕЛЬНО и подписан."""
    flags = flags if flags is not None else _flags()
    ideas = pathlib.Path(ideas_dir or IDEAS)
    sealed = {str(p.get("idea_file")): p for p in _jsonl(pred_path or PRED) if p.get("kind") == "hunter_idea"}
    deliv = _jsonl(deliv_path or DELIV)
    delivered = {}
    for d in deliv:
        if str(d.get("ref") or "").endswith(".push.txt"):
            delivered.setdefault(d["ref"], []).append(d)
    claims = sorted((ideas / "claims").glob("*.txt")) if (ideas / "claims").exists() else []
    hunts = []
    for c in claims:
        day = c.stem
        files = sorted(ideas.glob(f"idea_{day}_*.push.txt"))
        none = ideas / f"idea_{day}_none.json"
        lines = [x for x in c.read_text(encoding="utf-8").splitlines() if x.strip()]
        if files:
            for f in files:
                rec, dl = sealed.get(f.name), delivered.get(f.name, [])
                kinds = sorted({x.get("kind") for x in dl})
                stage = ("доставлено и записано в счёт" if rec and dl else
                         "доставлено «для обсуждения», в счёт не записано" if dl else
                         "файл есть, доставки нет — проверь алерты протяжки/суда")
                hunts.append({"дата": day, "запросов": len(lines), "итог": f.name, "этап": stage,
                              "доставки": kinds, "записано": bool(rec)})
        elif none.exists():
            hunts.append({"дата": day, "запросов": len(lines), "итог": none.name,
                          "этап": "охота завершилась честным нулём (защищаемой идеи нет)"})
        else:
            hunts.append({"дата": day, "запросов": len(lines), "итог": None,
                          "этап": "заявка есть, результата нет — охота не завершена или не закоммичена"})
    seen = {h.get("итог") for h in hunts}
    for name, rec in sorted(sealed.items()):          # идеи до журнала заявок (охота 02.11 и раньше)
        if name not in seen:
            dl = delivered.get(name, [])
            hunts.append({"дата": name[5:15], "запросов": None, "итог": name,
                          "этап": "доставлено и записано в счёт" if dl or rec.get("sealed_at") else "записано",
                          "доставки": sorted({x.get("kind") for x in dl}) or ["до журнала доставок (03.11)"],
                          "записано": True})
    dd_on = bool(flags.get("deep_dive_auto"))
    queue = []
    for q in _jsonl(deep_path or DEEP):
        if q.get("status") == "queued":
            queue.append({"актив": q.get("asset"), "нажато": str(q.get("ts"))[:16], "статус": (
                "в очереди; прокачка включена — пойдёт в ближайший прогон после 09:40 UTC" if dd_on else
                "ЖДЁТ: двухдневная прокачка ВЫКЛЮЧЕНА (config/presentation.yaml deep_dive_auto: false) до «ок» "
                "владельца после ручного показа; сама не запустится"),
                "как_запустить": "на сервере: cd /home/oracle/oracle && sudo -u oracle sh -c 'set -a && . ./.env && "
                                 "set +a && .venv/bin/python orchestrator/deep_dive.py --show'"})
    fdir = pathlib.Path(funnel_dir or FUNNEL)
    efs = sorted(fdir.glob("ef_*.json")) if fdir.exists() else []
    old = None
    if efs:
        old = {"последний_прогон": efs[-1].stem,
               "что_это": "СТАРЫЙ утренний конвейер event_first (картограф → граф → суд), НЕ охотник",
               "доставка": ("утреннее наблюдение уходит владельцу" if flags.get("server_observation_push")
                            else "утренний пуш ВЫКЛЮЧЕН (server_observation_push: false) — его кандидаты "
                                 "владельцу не уходят и работу охотника не подтверждают")}
    return {"охота_по_запросу": hunts, "прокачка_«Качаем»": queue, "старый_конвейер": old,
            "флаги": {k: flags.get(k) for k in ("daily_hunt", "deep_dive_auto", "week_odds_auto",
                                                 "week_odds_push", "server_observation_push")},
            "источник": "reports/ideas/claims, reports/ideas/*, journal/predictions|deliveries|deep_dive, "
                        "config/presentation.yaml"}


def event_reaction(ticker, event_time_utc, closes_fn):
    """Время события → сессия Нью-Йорка, ход ДО события и ход РЕАКЦИИ (mathlib.event_timing).
    closes_fn(ticker, date_from, date_to) → {YYYY-MM-DD: close}."""
    from mathlib import event_timing as ET
    c = ET.classify(event_time_utc)
    d0 = datetime.date.fromisoformat(c["last_close_before"]) - datetime.timedelta(days=10)
    d1 = datetime.date.fromisoformat(c["reaction_day"]) + datetime.timedelta(days=1)
    closes = closes_fn(ticker, d0.isoformat(), d1.isoformat()) or {}
    r = ET.reaction(event_time_utc, closes)
    r["ticker"] = ticker
    return r
