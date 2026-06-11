#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Панель контроля бюджета «Оракул» (MASTER_SPEC §11, §12, §30, инвариант 5 CLAUDE.md).

РЕШЕНИЕ Недели 1 (закрытие техдолга): ИСТОЧНИК ПРАВДЫ месячного спенда «Оракула» —
СУММА journal/costs.jsonl (что реально потратил наш оркестратор через OpenRouter). Цифра
/api/v1/key (usage_monthly) — ТОЛЬКО справочная строка «весь ключ»: ключ может расходоваться и
помимо наших прогонов, поэтому алерты по ней НЕ выставляются (иначе чужой спенд останавливал бы
«Оракула»). Алерты 80%/100% — исключительно по спенду «Оракула» против потолков config/limits.yaml.

Потолки НЕ хардкодятся — читаются из config/limits.yaml (budget.*). Так панель и зашитые в код
проверки оркестратора всегда смотрят на один и тот же потолок.

Три раздельные величины в панели и однострочном статусе:
  (1) Спенд «Оракула» (журнал)         — источник правды, по нему алерты;
  (2) Спенд всего ключа (справка)        — usage_monthly из /api/v1/key, без алертов;
  (3) Дневной остаток лимита ключа       — limit_remaining/limit из /api/v1/key (жёсткий стоп провайдера).

Выход: dashboard/budget.html + однострочная сводка в stdout (для Telegram/cron).
Код возврата: 0 — норма/внимание; 3 — спенд «Оракула» достиг потолка (прогоны стоп).
"""
import json
import os
import sys
import datetime
import pathlib
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIMITS_YAML = ROOT / "config" / "limits.yaml"


def load_budget_limits(path=LIMITS_YAML):
    """Потолки из config/limits.yaml (budget.*) — НЕ хардкод."""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    b = cfg["budget"]
    return {
        "tokens_usd_month": float(b["tokens_usd_month"]),
        "data_usd_month": float(b["data_usd_month"]),
        "total_usd_month": float(b["total_usd_month"]),
        "alert_fraction": float(b.get("alert_fraction", 0.8)),
        "costs_log": ROOT / b.get("costs_log", "journal/costs.jsonl"),
    }


def oracle_monthly_spend(costs_log, month=None):
    """ИСТОЧНИК ПРАВДЫ: сумма cost_usd из journal/costs.jsonl за месяц (mode!=mock).

    Возвращает (total_tokens_usd, by_mode, by_model). Строки mock (нулевая стоимость,
    дымовые тесты) исключаются. Поле стоимости — 'cost_usd'.
    """
    month = month or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
    total, by_mode, by_model = 0.0, {}, {}
    p = pathlib.Path(costs_log)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("mode") == "mock":
                continue
            if not str(rec.get("ts", "")).startswith(month):
                continue
            c = float(rec.get("cost_usd") or 0.0)
            total += c
            by_mode[rec.get("mode", "?")] = by_mode.get(rec.get("mode", "?"), 0.0) + c
            by_model[rec.get("model", "?")] = by_model.get(rec.get("model", "?"), 0.0) + c
    return total, by_mode, by_model


def key_reference():
    """СПРАВКА (без алертов): спенд всего ключа и дневной остаток лимита из /api/v1/key."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"error": "OPENROUTER_API_KEY не задан"}
    req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r).get("data", {})
    except Exception as e:
        return {"error": str(e)}
    return {
        "usage_monthly": d.get("usage_monthly"),
        "usage_daily": d.get("usage_daily"),
        "usage_weekly": d.get("usage_weekly"),
        "limit": d.get("limit"),
        "limit_remaining": d.get("limit_remaining"),
        "limit_reset": d.get("limit_reset"),
    }


def compute_status(oracle_tokens_usd, limits):
    """Алерты ТОЛЬКО по спенду «Оракула» (журнал) против потолков limits.yaml.

    tokens-фракция — главный индикатор; total включает фиксированный data_usd_month (§30).
    Статус: OK (<alert) / ВНИМАНИЕ (>=alert, <100%) / ПРЕВЫШЕНИЕ (>=100% → прогоны стоп).
    """
    cap_tok = limits["tokens_usd_month"]
    cap_total = limits["total_usd_month"]
    alert = limits["alert_fraction"]

    tok_frac = oracle_tokens_usd / cap_tok if cap_tok else 0.0
    total_spend = oracle_tokens_usd + limits["data_usd_month"]
    total_frac = total_spend / cap_total if cap_total else 0.0
    worst = max(tok_frac, total_frac)  # перешагнули любой потолок — алерт

    if worst >= 1.0:
        status, code = "ПРЕВЫШЕНИЕ — прогоны стоп", 3
    elif worst >= alert:
        status, code = f"ВНИМАНИЕ ≥{alert*100:.0f}%", 0
    else:
        status, code = "OK", 0
    return {
        "oracle_tokens_usd": round(oracle_tokens_usd, 4),
        "tokens_cap": cap_tok, "tokens_frac": tok_frac,
        "oracle_total_usd": round(total_spend, 2), "total_cap": cap_total, "total_frac": total_frac,
        "alert_fraction": alert, "status": status, "exit_code": code,
    }


def one_liner(st, key):
    """Однострочник: раздельно (1) Оракул-журнал, (2) ключ-справка, (3) дневной остаток."""
    tok = f"Оракул токены ${st['oracle_tokens_usd']:.2f}/${st['tokens_cap']:.0f} ({st['tokens_frac']*100:.0f}%)"
    tot = f"всего ~${st['oracle_total_usd']:.0f}/${st['total_cap']:.0f} ({st['total_frac']*100:.0f}%)"
    if "error" in key:
        ref = f"справка ключ: недоступна ({key['error']})"
    else:
        um = key.get("usage_monthly")
        lr, lim, reset = key.get("limit_remaining"), key.get("limit"), key.get("limit_reset")
        um_s = f"${um:.2f}/мес" if isinstance(um, (int, float)) else "н/д"
        rem_s = (f"остаток {reset or '?'} ${lr:.2f}/${lim:.0f}"
                 if isinstance(lr, (int, float)) and isinstance(lim, (int, float)) else "остаток н/д")
        ref = f"справка ключ: {um_s}, {rem_s} (без алертов)"
    return f"[бюджет] {st['status']}: {tok} · {tot} || {ref}"


def _bar(frac, alert):
    frac = max(0.0, min(frac, 1.2))
    color = "#2e7d32" if frac < alert else ("#f9a825" if frac < 1.0 else "#c62828")
    return (f'<div style="background:#eee;border-radius:6px;height:18px;width:100%">'
            f'<div style="width:{min(frac,1)*100:.0f}%;background:{color};height:18px;border-radius:6px"></div></div>')


def render_html(st, key, by_mode, by_model):
    a = st["alert_fraction"]
    if "error" in key:
        ref_block = f"<p style='color:#c62828'>OpenRouter /api/v1/key: {key['error']}</p>"
    else:
        um = key.get("usage_monthly"); ud = key.get("usage_daily"); uw = key.get("usage_weekly")
        lr = key.get("limit_remaining"); lim = key.get("limit"); reset = key.get("limit_reset")
        def money(x): return f"${x:.2f}" if isinstance(x, (int, float)) else "н/д"
        ref_block = f"""
<h3>(2) Спенд ВСЕГО КЛЮЧА — справка, БЕЗ алертов</h3>
<p>Месяц: <b>{money(um)}</b> · неделя: {money(uw)} · день: {money(ud)}
<br><span style="color:#777">ключ расходуется и помимо «Оракула»; по этой цифре алерты НЕ ставятся
(источник правды спенда «Оракула» — журнал ниже).</span></p>
<h3>(3) Дневной лимит ключа (жёсткий стоп провайдера)</h3>
<p>Остаток: <b>{money(lr)}</b> из {money(lim)} · сброс: {reset or '?'}</p>"""

    rows_mode = "".join(f"<tr><td>{m}</td><td style='text-align:right'>${v:.4f}</td></tr>"
                        for m, v in sorted(by_mode.items(), key=lambda x: -x[1]))
    rows_model = "".join(f"<tr><td>{m}</td><td style='text-align:right'>${v:.4f}</td></tr>"
                         for m, v in sorted(by_model.items(), key=lambda x: -x[1])[:12])
    html = f"""<!doctype html><meta charset="utf-8"><title>Оракул — бюджет</title>
<body style="font-family:system-ui;max-width:780px;margin:30px auto;padding:0 16px">
<h2>Бюджет «Оракул» — {datetime.date.today()}</h2>
<p style="font-size:18px"><b>{st['status']}</b> (алерты только по спенду «Оракула» против limits.yaml)</p>

<h3>(1) Спенд «ОРАКУЛА» — источник правды (journal/costs.jsonl), по нему алерты</h3>
<p>Токены OpenRouter: <b>${st['oracle_tokens_usd']:.2f}</b> / ${st['tokens_cap']:.0f} в месяц
({st['tokens_frac']*100:.0f}%)</p>{_bar(st['tokens_frac'], a)}
<p>Весь бюджет (токены + данные ${st['oracle_total_usd']-st['oracle_tokens_usd']:.0f}):
<b>${st['oracle_total_usd']:.0f}</b> / ${st['total_cap']:.0f} ({st['total_frac']*100:.0f}%)</p>{_bar(st['total_frac'], a)}
<table border=0 cellpadding=4><tr><th align=left>режим</th><th>$</th></tr>{rows_mode or '<tr><td>пока пусто</td></tr>'}</table>
<h4>Топ моделей по стоимости</h4>
<table border=0 cellpadding=4>{rows_model or '<tr><td>пока пусто</td></tr>'}</table>
{ref_block}
<p style="color:#777;margin-top:24px">Потолки из config/limits.yaml (budget.*). Жёсткий потолок
провайдера — limit на выделенном ключе; панель и оркестратор смотрят на один и тот же config.</p>
</body>"""
    return html


def main():
    limits = load_budget_limits()
    oracle_tokens, by_mode, by_model = oracle_monthly_spend(limits["costs_log"])
    key = key_reference()
    st = compute_status(oracle_tokens, limits)

    out = ROOT / "dashboard"
    out.mkdir(exist_ok=True)
    (out / "budget.html").write_text(render_html(st, key, by_mode, by_model), encoding="utf-8")

    print(one_liner(st, key))
    sys.exit(st["exit_code"])


if __name__ == "__main__":
    main()
