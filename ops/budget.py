#!/usr/bin/env python3
"""Панель контроля бюджета «Оракул».
ВАЖНО (корпоративный OpenRouter): все измерения и лимиты — ПО КЛЮЧУ, не по аккаунту.
Используется выделенный ключ проекта (OPENROUTER_API_KEY = ключ «oracle» с limit $500/мес).
/api/v1/key возвращает спенд именно этого ключа; /api/v1/credits (общий баланс организации)
НЕ опрашиваем и никаких настроек уровня аккаунта не трогаем.
Три источника: (1) OpenRouter key API — реальный спенд день/неделя/месяц по ключу;
(2) локальный journal/costs.jsonl — стоимость по прогонам/режимам/агентам;
(3) config/limits.yaml — утверждённые потолки ($500 токены, $200 данные, $700 всего).
Выход: dashboard/budget.html + однострочная сводка в stdout (для Telegram/cron)."""
import json, os, sys, datetime, urllib.request, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIMITS = {"tokens_usd": 500.0, "data_usd": 200.0, "total_usd": 700.0}  # из §30; синхронизировать с config/limits.yaml
ALERT = (0.8, 1.0)

def openrouter_key_status():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"error": "OPENROUTER_API_KEY не задан"}
    req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("data", {})
    except Exception as e:
        return {"error": str(e)}

def local_costs():
    """journal/costs.jsonl: {"ts": iso, "mode": str, "agent": str, "model": str, "cost": float}"""
    f = ROOT / "journal" / "costs.jsonl"
    now = datetime.datetime.now(datetime.timezone.utc)
    month = now.strftime("%Y-%m")
    by_mode, by_model, total = {}, {}, 0.0
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not str(rec.get("ts", "")).startswith(month):
                continue
            c = float(rec.get("cost", 0))
            total += c
            by_mode[rec.get("mode", "?")] = by_mode.get(rec.get("mode", "?"), 0) + c
            by_model[rec.get("model", "?")] = by_model.get(rec.get("model", "?"), 0) + c
    return total, by_mode, by_model

def bar(frac):
    frac = max(0.0, min(frac, 1.2))
    color = "#2e7d32" if frac < ALERT[0] else ("#f9a825" if frac < ALERT[1] else "#c62828")
    return f'<div style="background:#eee;border-radius:6px;height:18px;width:100%"><div style="width:{min(frac,1)*100:.0f}%;background:{color};height:18px;border-radius:6px"></div></div>'

def main():
    ors = openrouter_key_status()
    local_total, by_mode, by_model = local_costs()
    # Спенд месяца: предпочитаем цифру OpenRouter, локальный журнал — детализация
    or_month = None
    for k in ("usage_monthly", "monthly_spend", "usage"):  # поле зависит от версии API
        if isinstance(ors.get(k), (int, float)):
            or_month = float(ors[k]); break
    spend = or_month if or_month is not None else local_total
    frac = spend / LIMITS["tokens_usd"]
    total_frac = (spend + LIMITS["data_usd"]) / LIMITS["total_usd"]

    rows_mode = "".join(f"<tr><td>{m}</td><td style='text-align:right'>${v:.2f}</td></tr>" for m, v in sorted(by_mode.items(), key=lambda x: -x[1]))
    rows_model = "".join(f"<tr><td>{m}</td><td style='text-align:right'>${v:.2f}</td></tr>" for m, v in sorted(by_model.items(), key=lambda x: -x[1])[:12])
    err = f"<p style='color:#c62828'>OpenRouter API: {ors['error']}</p>" if "error" in ors else ""
    html = f"""<!doctype html><meta charset="utf-8"><title>Оракул — бюджет</title>
<body style="font-family:system-ui;max-width:760px;margin:30px auto;padding:0 16px">
<h2>Бюджет «Оракул» — {datetime.date.today()}</h2>{err}
<h3>Токены OpenRouter: ${spend:.2f} / ${LIMITS['tokens_usd']:.0f} в месяц</h3>{bar(frac)}
<h3>Весь бюджет (токены + данные ${LIMITS['data_usd']:.0f}): / ${LIMITS['total_usd']:.0f}</h3>{bar(total_frac)}
<p>Источник цифры месяца: {"OpenRouter /api/v1/key" if or_month is not None else "локальный journal/costs.jsonl (OpenRouter недоступен)"};
расхождение OR/локально: ${abs((or_month or 0)-local_total):.2f}</p>
<h3>По режимам (локальный журнал, месяц)</h3><table border=0 cellpadding=4>{rows_mode or '<tr><td>пока пусто</td></tr>'}</table>
<h3>Топ моделей по стоимости</h3><table border=0 cellpadding=4>{rows_model or '<tr><td>пока пусто</td></tr>'}</table>
<p style="color:#777">Жёсткий потолок — limit на ВЫДЕЛЕННОМ ключе «oracle»: при сбое панели остановится только этот ключ, корпоративный аккаунт и ключи коллег не затронуты.</p>
</body>"""
    out = ROOT / "dashboard"; out.mkdir(exist_ok=True)
    (out / "budget.html").write_text(html, encoding="utf-8")

    status = "OK" if frac < ALERT[0] else ("ВНИМАНИЕ ≥80%" if frac < ALERT[1] else "ПРЕВЫШЕНИЕ — прогоны стоп")
    print(f"[бюджет] {status}: токены ${spend:.2f}/${LIMITS['tokens_usd']:.0f} ({frac*100:.0f}%), всего ~${spend+LIMITS['data_usd']:.0f}/${LIMITS['total_usd']:.0f}")
    sys.exit(0 if frac < ALERT[1] else 3)  # код 3 = превышение, ловится cron-обёрткой/ботом

if __name__ == "__main__":
    main()
