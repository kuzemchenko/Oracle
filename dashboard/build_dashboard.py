# -*- coding: utf-8 -*-
"""dashboard/build_dashboard.py — дашборд наблюдаемости (MASTER_SPEC §15).

§15 требует на дашборде ВСЕ метрики:
  1. калибровочная кривая по корзинам (Brier);
  2. hit rate и P&L по школам / источникам / типам идей;
  3. «стоимость инсайта» — затраты на прогон и на идею;
  4. статус воронки (последний прогон §6);
  5. журнал обращений к holdout (бюджет 4/год, §25);
  6. реестр версий весов и pinned-моделей;
  7. счётчики ПРЕДОТВРАЩЁННЫХ ошибок — плохие ставки, от которых система удержала (§2: самое
     прибыльное решение большинства дней — не сделать плохую ставку).

Честность (П8): где форвард-данных ещё нет (этап Бумаги §11 не начат — нет разрешённых
исходов), метрика показывает «накапливается», а НЕ выдуманный ноль/число. Калибровка,
hit rate и P&L питаются только разрешёнными исходами; пока их нет — каркас с пометкой.

Всё детерминированно (инвариант 6): метрики считаются кодом из журналов и конфигов.
Запуск: python3 dashboard/build_dashboard.py  →  dashboard/index.html + dashboard/metrics.json
"""
import json
import html
import pathlib
import datetime

import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402
from mathlib import brier as B          # noqa: E402
from mathlib import sealing             # noqa: E402
from mathlib import outcomes as OUT     # noqa: E402
from orchestrator import resolve as RES  # noqa: E402  (join прогноз↔исход по hash, ревью 04.07 H1)
from ops import budget as BUD           # noqa: E402

FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"
OUT_HTML = ROOT / "dashboard" / "index.html"
OUT_JSON = ROOT / "dashboard" / "metrics.json"
# Д2 (ROADMAP_2026-07, решение владельца 13.07 Вопрос 2): табло §15 показывает ДВА ряда —
# официальный (из outcomes.jsonl, ПЕРВИЧНЫЙ) + диагностический из отчёта Д2 (только чтение).
# Файла нет → поведение табло прежнее байт-в-байт (ряд просто не показывается).
D2_DIAGNOSIS_JSON = ROOT / "ops" / "reports" / "d2_diagnosis" / "report.json"


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Сбор прогонов воронки ────────────────────────────────────────────────────────
def _load_funnel_runs():
    runs = []
    for p in sorted(FUNNEL_LOGS.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        runs.append(d)
    return runs


def _d2_diagnostic_row(path=None):
    """Диагностический ряд Д2 для табло §15 (решение владельца 13.07, Вопрос 2).
    Читается из ops/reports/d2_diagnosis/report.json (генерирует ops/diagnose_calibration.py);
    файла нет / битый / нет блока dashboard_row → None (официальный ряд остаётся единственным,
    поведение прежнее). Журналы этим НЕ трогаются — ряд только читает отчёт."""
    p = pathlib.Path(path) if path else D2_DIAGNOSIS_JSON
    if not p.exists():
        return None
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    row = rep.get("dashboard_row")
    if not isinstance(row, dict) or row.get("n") is None:
        return None
    verdict = rep.get("вердикт") or {}
    return {
        "источник": str(p),
        "n": row.get("n"),
        "hit_rate": row.get("hit_rate"),
        "brier": row.get("brier"),
        "пометка": row.get("пометка") or "диагностический ряд после разбора Д2",
        "баг_сверки_подтверждён": verdict.get("баг_сверки_подтверждён"),
        "статус": "диагностический (после разбора Д2); официальный ряд из outcomes.jsonl первичен",
    }


# ── (1) Калибровочная кривая по корзинам ─────────────────────────────────────────
def metric_calibration():
    recs = [r for r in sealing.read_predictions() if r.get("tag") != "test"]
    # ревью 04.07 H1: исходы живут в outcomes.jsonl (predictions append-only и исходов не содержит) —
    # без join по hash калибровка вечно «накапливается» при реально идущей сверке
    outs_map = RES.outcomes_by_hash()
    resolved = []
    for pred in recs:
        o = outs_map.get(pred.get("hash")) or {}
        res = OUT.resolve_prediction(pred, o.get("observed_value"), o.get("observed_at"))
        resolved.append(res)
    probs, outs = OUT.to_brier_inputs(resolved)
    if not probs:
        out = {"статус": "накапливается", "n_разрешённых": 0,
               "пояснение": "этап Бумаги §11 не начат — разрешённых форвард-исходов нет (П8); "
                            "каркас корзин ниже наполнится по мере сверки исходов",
               "корзины": [{"lo": i / 10, "hi": (i + 1) / 10, "n": 0,
                            "mean_pred": None, "obs_freq": None, "gap": None} for i in range(10)],
               "brier": None, "band_pp": None}
    else:
        band = B.calibration_band_pp(probs, outs)   # None, пока ни одна корзина не набрала MIN_BIN_N (H2)
        out = {"статус": "есть данные", "n_разрешённых": len(probs),
               "корзины": B.calibration_table(probs, outs),
               "brier": round(B.brier_score(probs, outs), 4),
               "band_pp": (None if band is None else round(band, 2))}
    # Д2: второй, диагностический ряд — ТОЛЬКО если отчёт Д2 существует (иначе прежнее поведение)
    d2 = _d2_diagnostic_row()
    if d2 is not None:
        out["диагностический_ряд_д2"] = d2
    return out


# ── (2) Hit rate и P&L по школам/источникам/типам ────────────────────────────────
def metric_hitrate_pnl():
    recs = [r for r in sealing.read_predictions() if r.get("tag") != "test"]
    outs_map = RES.outcomes_by_hash()               # ревью 04.07 H1: join прогноз↔исход по hash
    resolved = [OUT.resolve_prediction(p, (outs_map.get(p.get("hash")) or {}).get("observed_value"),
                                       (outs_map.get(p.get("hash")) or {}).get("observed_at"))
                for p in recs]
    n_res = sum(1 for r in resolved if r["status"] == "resolved")
    dims = ["школы", "источники", "типы_идей"]
    if n_res == 0:
        return {"статус": "накапливается", "n_разрешённых": 0,
                "пояснение": "нет разрешённых исходов — hit rate/P&L по " + "/".join(dims)
                             + " наполнятся на этапе Бумаги (П8: не выдумываем)",
                "разрезы": {d: {} for d in dims}}
    hits = sum(1 for r in resolved if r["status"] == "resolved" and r["outcome"] == 1)
    return {"статус": "есть данные", "n_разрешённых": n_res,
            "hit_rate_общий": round(hits / n_res, 3),
            "разрезы": {d: {} for d in dims},
            "пояснение": "разрезы наполняются по мере накопления помеченных исходов"}


# ── (3) Стоимость инсайта ────────────────────────────────────────────────────────
def metric_cost_of_insight(funnel_runs):
    lim = BUD.load_budget_limits()
    total, by_mode, _ = BUD.oracle_monthly_spend(lim["costs_log"])
    live_runs = [r for r in funnel_runs if r.get("mode") == "live"]
    ideas = sum(((r.get("воронка_отсева") or {}).get("этап6_выдано_топ") or 0) for r in live_runs)  # M15: явный None в логе
    n_live = len(live_runs)
    return {
        "месячный_спенд_usd": round(total, 4),
        "потолок_usd_мес": lim["total_usd_month"],
        "live_прогонов_в_журнале": n_live,
        "идей_выдано": ideas,
        "стоимость_на_прогон_usd": round(total / n_live, 4) if n_live else None,
        "стоимость_на_идею_usd": round(total / ideas, 4) if ideas else None,
        "пояснение": ("идей ещё не выдано — стоимость/идею не определена (П8)" if not ideas else
                      "стоимость инсайта = месячный спенд ÷ выданные идеи"),
        "разбивка_по_режимам": {k: round(v, 4) for k, v in by_mode.items()},
    }


# ── (4) Статус воронки (последний прогон) ────────────────────────────────────────
def metric_funnel_status(funnel_runs):
    if not funnel_runs:
        return {"статус": "нет прогонов"}
    last = max(funnel_runs, key=lambda r: r.get("ts", ""))
    fr = last.get("воронка_отсева") or {}
    return {
        "последний_run_id": last.get("run_id"), "ts": last.get("ts"), "mode": last.get("mode"),
        "тема": last.get("theme"),
        "скан_сырых": fr.get("этап1_сырых_сигналов"),
        "после_FDR": fr.get("этап1_сигналов_после_FDR"),
        "кандидатов": fr.get("этап2_кандидатов"),
        "в_дебаты": fr.get("этап4_в_дебаты_топ"),
        "устояло": fr.get("этап5_устояло_после_дебатов"),
        "выдано": fr.get("этап6_выдано_топ"),
        "вывод": fr.get("вывод"),
    }


# ── (5) Журнал обращений к holdout ───────────────────────────────────────────────
def metric_holdout():
    lim = _load_yaml(ROOT / "config" / "limits.yaml")
    hcfg = lim.get("holdout", {})
    budget = hcfg.get("access_budget_per_year", 4)
    log_path = ROOT / hcfg.get("log", "journal/holdout_access.log")
    used, entries = 0, []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                used += 1
                entries.append(line.strip())
    return {"бюджет_в_год": budget, "использовано": used, "осталось": budget - used,
            "записи": entries[-5:],
            "пояснение": "обращение к holdout — только для подтверждения, что система улучшилась,"
                         " а не подогналась (§25); каждое обращение журналируется"}


# ── (6) Реестр версий весов и pinned-моделей ─────────────────────────────────────
def metric_registry():
    w = _load_yaml(ROOT / "config" / "weights.yaml")
    m = _load_yaml(ROOT / "config" / "models.yaml")
    r = _load_yaml(ROOT / "config" / "rubric.yaml")
    return {
        "веса_версия": w.get("version"), "веса_created": w.get("created"),
        "pinned_quarter": (m.get("meta") or {}).get("pinned_quarter"),
        "рубрика_версия": r.get("version"),
        "пояснение": "версия весов растёт только через /apply-weights (§10); pinned-модели "
                     "обновляются ежеквартально с regression на masked_cases (§25)",
    }


# ── (7) Счётчики предотвращённых ошибок ──────────────────────────────────────────
def metric_prevented_errors(funnel_runs):
    """Плохие ставки, от которых система удержала (§2). По всем прогонам воронки:
    отсев FDR + грубый фильтр (тайминг/манипуляция) + разбито/вето в дебатах + процедурное вето
    + «тонкие дни» (прогон, не выдавший ни одной идеи)."""
    fdr, coarse, debate, veto, thin_days = 0, 0, 0, 0, 0
    per_run = []
    for r in funnel_runs:
        fr = r.get("воронка_отсева") or {}
        f = (fr.get("этап1_сырых_сигналов") or 0) - (fr.get("этап1_сигналов_после_FDR") or 0)
        c = fr.get("этап3_отсеяно") or 0
        d = fr.get("этап5_разбито_или_вето") or 0
        v = len(r.get("процедурное_вето") or [])
        issued = fr.get("этап6_выдано_топ")
        fdr += max(f, 0); coarse += c; debate += d; veto += v
        if issued == 0:
            thin_days += 1
        per_run.append({"run_id": r.get("run_id"), "mode": r.get("mode"),
                        "FDR": max(f, 0), "грубый_фильтр": c, "разбито_вето_дебаты": d,
                        "процедурное_вето": v, "идей_выдано": issued})
    total = fdr + coarse + debate + veto
    return {
        "всего_предотвращённых": total,
        "из_них": {"отсев_FDR": fdr, "грубый_фильтр_тайминг_манипуляция": coarse,
                   "разбито_или_вето_в_дебатах": debate, "процедурное_вето_П8": veto},
        "тонких_дней_без_идей": thin_days,
        "по_прогонам": per_run,
        "пояснение": "самое прибыльное решение большинства дней — не сделать плохую ставку (§2). "
                     "Учтены все прогоны журнала (на Нед.8 — тестовые/калибровочные).",
    }


def metric_cascade_trees():
    """Деревья каскадов последнего event-first прогона (§5/П5): событие→шок→узлы (амплитуда/P),
    разделение брандмауэром §9: запечатываемо vs лист ожидания. «накапливается», если прогонов нет."""
    files = sorted([p for p in FUNNEL_LOGS.glob("ef_*.json") if "__" not in p.name],
                   key=lambda p: p.stat().st_mtime)
    if not files:
        return {"status": "накапливается", "trees": [], "events": []}
    try:
        d = json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"status": "накапливается", "trees": [], "events": []}
    trees = []
    for s in d.get("по_источникам", []):
        cr = s.get("каскад_резолв") or {}
        nodes = []
        for sp in (cr.get("запечатываемо") or []):
            pr = sp.get("prediction", {})
            nodes.append({"актив": pr.get("asset"), "направление": pr.get("direction"),
                          "P": pr.get("probability"), "амплитуда": pr.get("amplitude_expected"),
                          "статус": "§9-прогноз"})
        for w in (cr.get("лист_ожидания") or []):
            nodes.append({"актив": w.get("актив"), "статус": "лист ожидания",
                          "причина": w.get("причина")})
        trees.append({"источник": s.get("источник"), "shock": s.get("shock"),
                      "контур_выдал": (s.get("контур") or {}).get("выдано"), "узлы": nodes})
    return {"status": "ok", "run_id": d.get("run_id"), "mode": d.get("mode"),
            "events": ((d.get("скан") or {}).get("топ_события") or [])[:6], "trees": trees}


# ── Сбор всех метрик ─────────────────────────────────────────────────────────────
def collect_metrics():
    runs = _load_funnel_runs()
    return {
        "сгенерировано": _now(),
        "spec_ref": "§15 наблюдаемость",
        "калибровка": metric_calibration(),
        "hit_rate_pnl": metric_hitrate_pnl(),
        "стоимость_инсайта": metric_cost_of_insight(runs),
        "статус_воронки": metric_funnel_status(runs),
        "holdout": metric_holdout(),
        "реестр_версий": metric_registry(),
        "предотвращённые_ошибки": metric_prevented_errors(runs),
        "деревья_каскадов": metric_cascade_trees(),
    }


# ── Рендер HTML ──────────────────────────────────────────────────────────────────
def _h(x):
    return html.escape(str(x))


def _card(title, body):
    return (f'<section style="background:#fff;border:1px solid #e3e3e3;border-radius:10px;'
            f'padding:16px 18px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.04)">'
            f'<h2 style="margin:0 0 10px;font-size:17px;color:#222">{title}</h2>{body}</section>')


def _accum_badge(status):
    if status == "накапливается":
        return ('<span style="background:#fff3cd;color:#8a6d00;border-radius:6px;'
                'padding:2px 8px;font-size:12px">накапливается (нет форвард-исходов)</span>')
    return ('<span style="background:#d4edda;color:#155724;border-radius:6px;'
            'padding:2px 8px;font-size:12px">есть данные</span>')


def _calibration_card(m):
    rows = "".join(
        f"<tr><td>{r['lo']:.1f}–{r['hi']:.1f}</td><td>{r['n']}</td>"
        f"<td>{'—' if r['mean_pred'] is None else round(r['mean_pred'],3)}</td>"
        f"<td>{'—' if r['obs_freq'] is None else round(r['obs_freq'],3)}</td>"
        f"<td>{'—' if r['gap'] is None else round(r['gap'],3)}</td></tr>"
        for r in m["корзины"])
    head = (f"{_accum_badge(m['статус'])} · разрешённых: <b>{m['n_разрешённых']}</b> · "
            f"Brier: {m.get('brier') if m.get('brier') is not None else '—'} · "
            f"полоса калибровки: {m.get('band_pp') if m.get('band_pp') is not None else '—'} п.п.")
    note = f"<p style='color:#666;font-size:13px'>{_h(m.get('пояснение',''))}</p>" if m.get("пояснение") else ""
    d2 = m.get("диагностический_ряд_д2")
    d2_html = ""
    if d2:
        d2_html = (
            "<div style='margin-top:10px;padding:8px 10px;background:#eef4fb;"
            "border:1px dashed #7aa7d9;border-radius:8px;font-size:13px'>"
            "<b>Диагностический ряд (после найденной ошибки Д2)</b> — официальный ряд выше "
            "первичен; этот пересчитан из сырых котировок отчётом Д2.<br>"
            f"n: <b>{_h(d2.get('n'))}</b> · hit rate: <b>{_h(d2.get('hit_rate'))}</b> · "
            f"Brier: <b>{_h(d2.get('brier'))}</b> · баг сверки подтверждён: "
            f"<b>{'ДА' if d2.get('баг_сверки_подтверждён') else 'НЕТ'}</b>"
            f"<p style='color:#666;margin:4px 0 0'>{_h(d2.get('пометка'))}</p></div>")
    return _card("1 · Калибровочная кривая по корзинам",
                 head + note +
                 "<table style='width:100%;border-collapse:collapse;font-size:13px' "
                 "border='1' cellpadding='4'>"
                 "<tr style='background:#f5f5f5'><th>корзина</th><th>n</th><th>pred</th>"
                 "<th>факт</th><th>разрыв</th></tr>" + rows + "</table>" + d2_html)


def _hitrate_card(m):
    body = f"{_accum_badge(m['статус'])} · разрешённых: <b>{m['n_разрешённых']}</b>"
    if m.get("hit_rate_общий") is not None:
        body += f" · hit rate: <b>{m['hit_rate_общий']:.1%}</b>"
    body += f"<p style='color:#666;font-size:13px'>{_h(m.get('пояснение',''))}</p>"
    body += "<p style='font-size:13px'>Разрезы: " + ", ".join(m["разрезы"].keys()) + "</p>"
    return _card("2 · Hit rate и P&L по школам / источникам / типам идей", body)


def _cost_card(m):
    rows = "".join(f"<li>{_h(k)}: ${v}</li>" for k, v in m["разбивка_по_режимам"].items())
    body = (f"Месячный спенд: <b>${m['месячный_спенд_usd']}</b> / потолок ${m['потолок_usd_мес']} · "
            f"live-прогонов: {m['live_прогонов_в_журнале']} · идей выдано: {m['идей_выдано']}<br>"
            f"<b>Стоимость на прогон:</b> "
            f"{'$'+str(m['стоимость_на_прогон_usd']) if m['стоимость_на_прогон_usd'] is not None else '—'} · "
            f"<b>на идею:</b> "
            f"{'$'+str(m['стоимость_на_идею_usd']) if m['стоимость_на_идею_usd'] is not None else '—'}"
            f"<p style='color:#666;font-size:13px'>{_h(m['пояснение'])}</p>"
            f"<ul style='font-size:13px;margin:4px 0'>{rows}</ul>")
    return _card("3 · Стоимость инсайта (затраты на прогон / идею)", body)


def _funnel_card(m):
    if m.get("статус") == "нет прогонов":
        return _card("4 · Статус воронки", "нет прогонов в журнале")
    body = (f"Последний: <b>{_h(m['последний_run_id'])}</b> ({_h(m['mode'])}, {_h(m['ts'])}) · "
            f"тема {_h(m['тема'])}<br>"
            f"скан {_h(m['скан_сырых'])} → FDR {_h(m['после_FDR'])} → кандидатов {_h(m['кандидатов'])} → "
            f"дебаты {_h(m['в_дебаты'])} → устояло {_h(m['устояло'])} → <b>выдано {_h(m['выдано'])}</b>"
            f"<p style='color:#444;font-size:13px'>{_h(m['вывод'])}</p>")   # M15: всё из логов — через _h
    return _card("4 · Статус воронки (последний прогон §6)", body)


def _holdout_card(m):
    body = (f"Использовано <b>{m['использовано']}</b> / {m['бюджет_в_год']} в год "
            f"(осталось {m['осталось']})"
            f"<p style='color:#666;font-size:13px'>{_h(m['пояснение'])}</p>")
    if m["записи"]:
        body += "<ul style='font-size:13px'>" + "".join(f"<li>{_h(e)}</li>" for e in m["записи"]) + "</ul>"
    return _card("5 · Журнал обращений к holdout", body)


def _registry_card(m):
    body = (f"Веса: версия <b>{_h(m['веса_версия'])}</b> (от {_h(m['веса_created'])}) · "
            f"рубрика версия <b>{_h(m['рубрика_версия'])}</b> · "
            f"pinned-модели квартал <b>{_h(m['pinned_quarter'])}</b>"
            f"<p style='color:#666;font-size:13px'>{_h(m['пояснение'])}</p>")
    return _card("6 · Реестр версий весов и pinned-моделей", body)


def _prevented_card(m):
    iz = m["из_них"]
    rows = "".join(
        f"<tr><td>{_h(r['run_id'])}</td><td>{_h(r['mode'])}</td><td>{r['FDR']}</td>"
        f"<td>{r['грубый_фильтр']}</td><td>{r['разбито_вето_дебаты']}</td>"
        f"<td>{r['процедурное_вето']}</td><td>{'—' if r['идей_выдано'] is None else r['идей_выдано']}</td></tr>"
        for r in m["по_прогонам"])
    body = (f"<div style='font-size:28px;font-weight:700;color:#155724'>{m['всего_предотвращённых']}</div>"
            f"плохих ставок, от которых система удержала · тонких дней без идей: "
            f"<b>{m['тонких_дней_без_идей']}</b><br>"
            f"<span style='font-size:13px'>FDR: {iz['отсев_FDR']} · "
            f"грубый фильтр: {iz['грубый_фильтр_тайминг_манипуляция']} · "
            f"разбито/вето в дебатах: {iz['разбито_или_вето_в_дебатах']} · "
            f"процедурное вето П8: {iz['процедурное_вето_П8']}</span>"
            f"<p style='color:#666;font-size:13px'>{_h(m['пояснение'])}</p>"
            "<table style='width:100%;border-collapse:collapse;font-size:12px' border='1' cellpadding='3'>"
            "<tr style='background:#f5f5f5'><th>прогон</th><th>режим</th><th>FDR</th><th>фильтр</th>"
            "<th>дебаты</th><th>вето</th><th>выдано</th></tr>" + rows + "</table>")
    return _card("7 · Счётчик предотвращённых ошибок", body)


def _cascade_card(m):
    if m.get("status") != "ok" or not m.get("trees"):
        return _card("8 · Деревья каскадов (event-first §5/П5)",
                     _accum_badge("накапливается") + " event-first прогонов ещё нет")
    blocks = []
    for t in m["trees"]:
        items = []
        for n in t["узлы"]:
            if n.get("статус") == "§9-прогноз":
                items.append(f"<li>✅ <b>{_h(n.get('актив'))}</b> {_h(n.get('направление'))} · "
                             f"P={_h(n.get('P'))} · ампл={_h(n.get('амплитуда'))} <i>(§9-прогноз)</i></li>")
            else:
                items.append(f"<li>⏳ <b>{_h(n.get('актив'))}</b> — лист ожидания "
                             f"<span style='color:#666'>({_h((n.get('причина') or '')[:45])})</span></li>")
        sh = t.get("shock")
        blocks.append(f"<p style='margin:8px 0 2px'><b>⚡ {_h(t['источник'])}</b> "
                      f"шок={_h(sh)} · контур выдал {_h(t.get('контур_выдал'))}</p>"
                      f"<ul style='margin:2px 0 8px'>{''.join(items) or '<li>узлов нет</li>'}</ul>")
    ev = ", ".join(_h(e) for e in m.get("events", []))
    head = (f"<div style='color:#666;font-size:13px'>{_h(m['run_id'])} · {_h(m['mode'])} · "
            f"события: {ev}</div>")
    return _card("8 · Деревья каскадов (event-first §5/П5)", head + "".join(blocks))


def render_html(metrics):
    cards = (_calibration_card(metrics["калибровка"]) +
             _hitrate_card(metrics["hit_rate_pnl"]) +
             _cost_card(metrics["стоимость_инсайта"]) +
             _funnel_card(metrics["статус_воронки"]) +
             _holdout_card(metrics["holdout"]) +
             _registry_card(metrics["реестр_версий"]) +
             _prevented_card(metrics["предотвращённые_ошибки"]) +
             _cascade_card(metrics["деревья_каскадов"]))
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Оракул · Дашборд §15</title></head>"
        "<body style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa;"
        "max-width:900px;margin:0 auto;padding:18px;color:#222'>"
        "<h1 style='font-size:22px'>Оракул · Наблюдаемость (§15)</h1>"
        f"<p style='color:#666;font-size:13px'>сгенерировано {_h(metrics['сгенерировано'])} · "
        "исследовательский инструмент, не инвестиционная рекомендация</p>"
        + cards +
        "<p style='color:#999;font-size:12px;margin-top:20px'>Все метрики посчитаны "
        "детерминированным кодом из журналов (инвариант 6). «Накапливается» = форвард-исходов "
        "ещё нет (этап Бумаги §11 не начат); числа не выдумываются (П8).</p>"
        "</body></html>")


def main():
    metrics = collect_metrics()
    OUT_JSON.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    OUT_HTML.write_text(render_html(metrics), encoding="utf-8")
    pe = metrics["предотвращённые_ошибки"]["всего_предотвращённых"]
    print(f"дашборд §15 → {OUT_HTML.relative_to(ROOT)} + {OUT_JSON.relative_to(ROOT)}")
    print(f"  калибровка: {metrics['калибровка']['статус']} · "
          f"предотвращённых ошибок: {pe} · "
          f"стоимость/идею: {metrics['стоимость_инсайта']['стоимость_на_идею_usd']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
