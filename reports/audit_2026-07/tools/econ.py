#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностический аудит экономики «Оракула» за июль 2026 (ТОЛЬКО ЧТЕНИЕ).

Источники:
  journal/costs.jsonl          — каждый LLM-вызов (ts, mode, agent, model, tokens, cost_usd, run_id)
  journal/funnel_logs/<rid>.json — исход прогона (поток_идей.money, контур_выдал_топ3, дайджест)
  config/limits.yaml           — потолки бюджета

Не делает сетевых вызовов, не трогает боевой код. Печатает разделы 1,2,4.
"""
import json, glob, collections, pathlib, yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
COSTS = ROOT / "journal" / "costs.jsonl"
FLOGS = ROOT / "journal" / "funnel_logs"
LIMITS = ROOT / "config" / "limits.yaml"
MONTH = "2026-07"


def load_costs():
    rows = []
    for line in COSTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if (d.get("ts") or "").startswith(MONTH):
            rows.append(d)
    return rows


def section1(rows):
    print("=" * 70)
    print("РАЗДЕЛ 1 — ИЮЛЬСКИЕ РАСХОДЫ")
    print("=" * 70)
    tot_c = sum(r.get("cost_usd", 0) or 0 for r in rows)
    tot_pt = sum(r.get("prompt_tokens", 0) or 0 for r in rows)
    tot_ct = sum(r.get("completion_tokens", 0) or 0 for r in rows)
    print(f"вызовов июля: {len(rows)}")
    print(f"суммарно $: {tot_c:.4f}")
    print(f"prompt_tokens: {tot_pt:,}  completion_tokens: {tot_ct:,}  всего токенов: {tot_pt+tot_ct:,}")

    def agg(key):
        c = collections.defaultdict(lambda: [0.0, 0, 0, 0])  # cost, ptok, ctok, calls
        for r in rows:
            k = r.get(key) or "?"
            c[k][0] += r.get("cost_usd", 0) or 0
            c[k][1] += r.get("prompt_tokens", 0) or 0
            c[k][2] += r.get("completion_tokens", 0) or 0
            c[k][3] += 1
        return sorted(c.items(), key=lambda kv: -kv[1][0])

    for key, title in [("mode", "ПО MODE"), ("model", "ПО MODEL"), ("agent", "ПО AGENT")]:
        print(f"\n--- {title} ---")
        for k, (co, pt, ct, n) in agg(key):
            print(f"  {co:8.4f}$  {pt+ct:>9,}tok  {n:>4}calls  {k}")
    return tot_c, tot_pt + tot_ct


def load_flow(rid):
    """Вернуть (money, top3_len, digest, found) для ef-прогона по funnel log."""
    p = FLOGS / f"{rid}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    pt = d.get("поток_идей") or {}
    money = pt.get("money", 0) or 0
    digest = pt.get("дайджест", 0) or 0
    top3 = d.get("контур_выдал_топ3")
    top3_len = len(top3) if isinstance(top3, (list, dict)) else (0 if not top3 else 1)
    return {"money": money, "top3": top3_len, "digest": digest}


def section2(rows):
    print("\n" + "=" * 70)
    print("РАЗДЕЛ 2 — ДОЛЯ $/ТОКЕНОВ НА ПУСТЫЕ ПРОГОНЫ")
    print("=" * 70)
    # агрегируем по run_id
    per_run = collections.defaultdict(lambda: [0.0, 0])
    for r in rows:
        rid = r.get("run_id") or "NONE"
        per_run[rid][0] += r.get("cost_usd", 0) or 0
        per_run[rid][1] += (r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0)

    cats = collections.defaultdict(lambda: [0.0, 0, 0])  # cost, tok, nruns
    empty_cost = empty_tok = 0.0
    have_flow_cost = have_flow_tok = 0.0
    detail = []
    for rid, (co, tk) in sorted(per_run.items()):
        fl = load_flow(rid)
        if fl is None:
            # нет funnel log: cross_review / challenge / bot_chat — не продуктовый прогон воронки
            kind = rid.split("_")[0]
            cats[f"нет_funnel_log:{kind}"][0] += co
            cats[f"нет_funnel_log:{kind}"][1] += tk
            cats[f"нет_funnel_log:{kind}"][2] += 1
            continue
        have_flow_cost += co
        have_flow_tok += tk
        empty = (fl["money"] == 0) and (fl["top3"] == 0)
        label = "ПУСТОЙ" if empty else "ВЫДАЛ"
        if empty:
            empty_cost += co
            empty_tok += tk
        cats[f"ef:{label}"][0] += co
        cats[f"ef:{label}"][1] += tk
        cats[f"ef:{label}"][2] += 1
        detail.append((rid, co, tk, fl, empty))

    print("Определение ПУСТОГО прогона: поток_идей.money==0 И контур_выдал_топ3 пуст.")
    print("\nef-прогоны (есть funnel log) — детально:")
    for rid, co, tk, fl, empty in detail:
        print(f"  {'ПУСТОЙ' if empty else 'ВЫДАЛ '}  {co:7.4f}$  money={fl['money']} top3={fl['top3']} digest={fl['digest']}  {rid}")

    print("\nСводка по категориям:")
    for k, (co, tk, n) in sorted(cats.items(), key=lambda x: -x[1][0]):
        print(f"  {co:8.4f}$  {tk:>9,}tok  {n:>3}runs  {k}")

    print(f"\n--- ДОЛЯ ПУСТЫХ СРЕДИ ТОЛЬКО ef-ПРОГОНОВ (продуктовая воронка) ---")
    if have_flow_cost:
        print(f"  ef пустые $: {empty_cost:.4f} из {have_flow_cost:.4f}  = {100*empty_cost/have_flow_cost:.1f}%")
        print(f"  ef пустые tok: {empty_tok:,.0f} из {have_flow_tok:,.0f}  = {100*empty_tok/have_flow_tok:.1f}%")

    tot_c = sum(r.get("cost_usd", 0) or 0 for r in rows)
    tot_t = sum((r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0) for r in rows)
    print(f"\n--- ДОЛЯ ПУСТЫХ ОТ ВСЕГО ИЮЛЬСКОГО СПЕНДА (весь costs.jsonl) ---")
    print(f"  пустые $: {empty_cost:.4f} из {tot_c:.4f}  = {100*empty_cost/tot_c:.1f}%")
    print(f"  пустые tok: {empty_tok:,.0f} из {tot_t:,.0f}  = {100*empty_tok/tot_t:.1f}%")
    return empty_cost, empty_tok, have_flow_cost, have_flow_tok, tot_c, tot_t


def section4(rows_all):
    print("\n" + "=" * 70)
    print("РАЗДЕЛ 4 — БЮДЖЕТ vs ЛИМИТЫ")
    print("=" * 70)
    cfg = yaml.safe_load(LIMITS.read_text(encoding="utf-8"))
    b = cfg["budget"]
    print(f"Потолки limits.yaml: tokens_usd_month={b['tokens_usd_month']} data={b['data_usd_month']} total={b['total_usd_month']} alert={b['alert_fraction']}")
    tot_c = sum(r.get("cost_usd", 0) or 0 for r in rows_all)
    print(f"\nСпенд «Оракула» (LLM/OpenRouter) за июль из costs.jsonl: ${tot_c:.4f}")
    print(f"  vs потолок токенов ${b['tokens_usd_month']}: {100*tot_c/b['tokens_usd_month']:.1f}%")
    print(f"  vs общий потолок ${b['total_usd_month']}: {100*tot_c/b['total_usd_month']:.1f}%")
    print("  (данные-подписки ~$200/мес в costs.jsonl НЕ логируются — это только LLM-вызовы)")


if __name__ == "__main__":
    rows = load_costs()
    section1(rows)
    section2(rows)
    section4(rows)
