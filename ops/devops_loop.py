# -*- coding: utf-8 -*-
"""ops/devops_loop.py — §R6 devops-ПЕТЛЯ-ПРЕДЛОЖИТЕЛЬ (Карпатый: петля обратной связи + ограничители
+ сохранить понимание).

АВТОНОМИЯ 0: детерминированно анализирует ТЕРРЕЙН (выход событий→каскады, отсев по воротам, треки,
добор, активации цепочек) и КАЛИБРОВКУ (Brier по трекам), и выдаёт ПРЕДЛОЖЕНИЯ (наблюдение → что
изменить → эффект) на ПОДПИСЬ владельца. САМ НИЧЕГО НЕ МЕНЯЕТ — ни весов, ни порогов, ни журналов.

П16-БРАНДМАУЭР: анализ только детерминированной машинерии и калибровки по факту — НЕ оптимизирует
под форвард-Brier (это переобучение). Веса/пороги/лимиты меняет ЧЕЛОВЕК по §10 (N≥30/значимость,
инвариант 5). Предложения — сырьё для решения, не автоприменение.
"""
import datetime
import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOGS = ROOT / "journal" / "funnel_logs"
PROPOSALS = ROOT / "journal" / "proposals"

GATE_DOMINANT = 0.45     # критерий валит ≥45% всего отсева → предложить пересмотр порога
GRADUATE_N = 30          # §10: N≥30 исходов для предложения выпуска/правки


def collect_runs(n=10, logs=LOGS):
    """Последние n СВОДНЫХ event-first прогонов (ef_*, без '__')."""
    files = [f for f in sorted(logs.glob("ef_*.json")) if "__" not in f.name][-n:]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return out


def terrain_metrics(runs):
    """Агрегаты террейна по прогонам (чистая функция — на вход загруженные протоколы)."""
    n = len(runs)
    if not n:
        return {"прогонов": 0}
    crit, chains = Counter(), Counter()
    nodes = fetched = 0
    tracks = {"money": 0, "провизорный": 0}
    for p in runs:
        g = p.get("граф_отбор") or {}
        nodes += g.get("узлов", 0) or 0
        for c, k in (g.get("отсев_по_критериям") or {}).items():
            crit[c] += k
        tr = g.get("треки") or {}
        tracks["money"] += tr.get("money", 0) or 0
        tracks["провизорный"] += tr.get("провизорный", 0) or 0
        fetched += len((g.get("добор_истории") or {}).get("скачано") or [])
        for c in (p.get("каскады_в_компании") or []):
            cid = c.get("chain_id")
            if cid:
                chains[cid] += 0 if c.get("пропуск") else 1
    total = sum(crit.values())
    return {"прогонов": n, "узлов_всего": nodes, "узлов_на_прогон": round(nodes / n, 1),
            "отсев_по_критериям": dict(crit),
            "доля_отсева": {c: round(k / total, 2) for c, k in crit.items()} if total else {},
            "треки": tracks, "тикеров_добрано": fetched, "цепочки_активаций": dict(chains)}


def calibration_state():
    """Калибровка по факту (resolve §10.10): Brier денежный/провизорный, до ворот 270. Read-only."""
    try:
        from orchestrator import resolve as R
        s = R.run_resolve(write=False)
        return {"brier_денежный": s.get("brier"), "исходов": s.get("всего_исходов"),
                "провизорный": s.get("провизорный_трек") or {}, "до_ворот_270": s.get("до_ворот_270")}
    except Exception as e:  # noqa: BLE001
        return {"ошибка": str(e)[:100]}


def generate_proposals(metrics, calib):
    """Детерминированные правила → список предложений. Каждое требует подписи (автономия 0)."""
    props = []
    for c, frac in (metrics.get("доля_отсева") or {}).items():
        if frac >= GATE_DOMINANT:
            props.append({
                "наблюдение": f"ворота «{c}» валят {int(frac * 100)}% отсева за {metrics.get('прогонов')} прогонов",
                "предложение": f"пересмотреть порог/логику ворот «{c}», если режут полезное",
                "эффект": "шире вход / меньше ложных отсевов (риск — больше шума)",
                "требует_подписи": True})
    for cid, acts in (metrics.get("цепочки_активаций") or {}).items():
        if acts == 0 and not str(cid).startswith("proposed:"):   # эфемерные карты картографа — не «мёртвые»
            props.append({
                "наблюдение": f"авторская цепочка «{cid}» активировалась 0 раз за выборку",
                "предложение": "проверить актуальность цепочки или её триггеры (тема/ценовой сигнал)",
                "эффект": "чистка мёртвого графа", "требует_подписи": True})
    pv = calib.get("провизорный") or {}
    if (pv.get("исходов") or 0) >= GRADUATE_N and pv.get("brier") is not None:
        props.append({
            "наблюдение": f"провизорный трек: {pv['исходов']} исходов, Brier={pv['brier']}",
            "предложение": "оценить ВЫПУСК откалиброванных каскад-методов в денежный трек (§R3, §10 N≥30)",
            "эффект": "гипотезы, доказавшие себя форвардом → к §11", "требует_подписи": True})
    if not props:
        props.append({"наблюдение": "явных аномалий террейна/калибровки не найдено",
                      "предложение": "ничего не менять", "эффект": "—", "требует_подписи": False})
    return props


def _markdown(report):
    L = [f"# Devops-предложения (§R6, автономия 0) — {report['ts']}",
         "", "_Анализ детерминированный; применяет ЧЕЛОВЕК по подписи (§10/инвариант 5/П16)._", ""]
    m = report["метрики"]
    L.append(f"**Террейн** ({m.get('прогонов', 0)} прогонов): узлов/прогон {m.get('узлов_на_прогон', '—')}, "
             f"треки money {m.get('треки', {}).get('money', 0)}/провиз {m.get('треки', {}).get('провизорный', 0)}, "
             f"добрано тикеров {m.get('тикеров_добрано', 0)}.")
    L.append(f"Отсев по критериям: {m.get('доля_отсева') or '—'}")
    c = report["калибровка"]
    L.append(f"**Калибровка**: Brier денеж={c.get('brier_денежный')}, "
             f"провиз={(c.get('провизорный') or {}).get('brier')} ({(c.get('провизорный') or {}).get('исходов', 0)} исх.), "
             f"до ворот 270: {c.get('до_ворот_270')}.")
    L.append("\n## Предложения (на подпись)")
    for i, p in enumerate(report["предложения"], 1):
        mark = "✍️ подпись" if p.get("требует_подписи") else "ℹ️"
        L.append(f"{i}. [{mark}] **{p['наблюдение']}**\n   → {p['предложение']}\n   эффект: {p['эффект']}")
    return "\n".join(L)


def run_devops_proposer(n=10, write=True, now_iso=None):
    """Полный проход: террейн + калибровка → предложения → отчёт (json+md в journal/proposals/)."""
    ts = now_iso or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    runs = collect_runs(n)
    metrics = terrain_metrics(runs)
    calib = calibration_state()
    report = {"тип": "devops-предложения §R6 (автономия 0 — применяет человек)", "ts": ts,
              "метрики": metrics, "калибровка": calib, "предложения": generate_proposals(metrics, calib),
              "дисциплина": "П16: анализ детерминир. машинерии/калибровки, НЕ форвард-Brier; "
                            "веса/пороги/лимиты — только предложение (§10, инвариант 5)"}
    if write:
        PROPOSALS.mkdir(parents=True, exist_ok=True)
        stamp = ts.replace(":", "").replace("-", "")[:15]
        (PROPOSALS / f"proposals_{stamp}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (PROPOSALS / f"proposals_{stamp}.md").write_text(_markdown(report), encoding="utf-8")
    return report


if __name__ == "__main__":
    r = run_devops_proposer()
    print(_markdown(r))
