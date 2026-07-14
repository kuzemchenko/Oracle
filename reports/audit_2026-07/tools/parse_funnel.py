#!/usr/bin/env python3
"""Funnel mortality table from ef_* top-level logs (event_first cron 09:00).
Read-only. No LLM. Aggregates stage-by-stage attrition over last 30 days."""
import json, glob, os, datetime

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
LOGS = os.path.join(BASE, 'journal', 'funnel_logs')

# last 30 days from 2026-07-14
CUTOFF = datetime.date(2026, 6, 14)

rows = []
for path in sorted(glob.glob(os.path.join(LOGS, 'ef_*.json'))):
    base = os.path.basename(path)
    # top-level only: ef_YYYYMMDDTHHMMSSZ.json (no __TICKER, no model_swaps)
    if '__' in base or 'model_swaps' in base:
        continue
    ts = base[3:3+15]  # 20260625T090001
    try:
        d = datetime.datetime.strptime(ts, '%Y%m%dT%H%M%S')
    except ValueError:
        continue
    if d.date() < CUTOFF:
        continue
    try:
        j = json.load(open(path))
    except Exception as e:
        rows.append({'file': base, 'error': str(e)})
        continue
    scan = j.get('скан', {}) or {}
    src = scan.get('источники', {}) or {}
    graf = j.get('граф_отбор', {}) or {}
    treki = graf.get('треки', {}) or {}
    seal = graf.get('запечатано', {}) or {}
    sud = graf.get('суд_money', {}) or {}
    top3 = j.get('контур_выдал_топ3', []) or []
    shock = j.get('шок_источники', []) or []
    cascades = j.get('каскады_в_компании', []) or []
    kartograf = j.get('картограф_идеи', []) or []
    # count court outcomes
    razbita = sum(1 for v in sud.values() if isinstance(v, dict) and v.get('исход') == 'РАЗБИТА')
    ustoyala = sum(1 for v in sud.values() if isinstance(v, dict) and v.get('исход') not in ('РАЗБИТА', None))
    rows.append({
        'file': base,
        'date': d.date().isoformat(),
        'hhmm': d.strftime('%H:%M'),
        'src_price': src.get('price'),
        'src_trends': src.get('trends'),
        'src_news': src.get('news_clusters'),
        'raw': scan.get('сырых_сигналов'),
        'after_fdr': scan.get('статистических_после_FDR'),
        'shock_src': len(shock),
        'cascades': len(cascades),
        'kartograf': len(kartograf),
        'graph_nodes': graf.get('узлов'),
        'gate_pass': graf.get('ворота_прошли'),
        'track_money': treki.get('money'),
        'track_prov': treki.get('провизорный'),
        'track_digest': treki.get('дайджест'),
        'sealed_money': seal.get('money'),
        'sealed_prov': seal.get('провизорный'),
        'demoted': seal.get('демотировано_судом'),
        'court_n': len(sud),
        'court_razbita': razbita,
        'court_ustoyala': ustoyala,
        'top3_out': len(top3),
    })

# only cron 09:xx runs for the main table, but keep all
cron = [r for r in rows if r.get('hhmm', '').startswith('09:')]

def s(x):
    return '' if x is None else str(x)

hdr = ['date','hhmm','raw','after_fdr','shock','casc','kart','nodes','gate','t_money','t_prov','seal_money','seal_prov','demot','court','razb','ustoy','top3']
keys = ['date','hhmm','raw','after_fdr','shock_src','cascades','kartograf','graph_nodes','gate_pass','track_money','track_prov','sealed_money','sealed_prov','demoted','court_n','court_razbita','court_ustoyala','top3_out']

print('=== ALL CRON 09:xx EF RUNS (last 30 days) ===')
print(' | '.join(hdr))
for r in cron:
    if 'error' in r:
        print(r['file'], 'ERROR', r['error']); continue
    print(' | '.join(s(r[k]) for k in keys))

print()
print('=== NON-CRON / OTHER RUNS (same window) ===')
for r in rows:
    if r.get('hhmm','').startswith('09:'): continue
    if 'error' in r:
        print(r['file'], 'ERROR', r['error']); continue
    print(' | '.join(s(r[k]) for k in keys))

# aggregate over cron runs
print()
print('=== AGGREGATE (cron 09:xx, n=%d) ===' % len(cron))
def agg(k):
    vals = [r[k] for r in cron if isinstance(r.get(k),(int,float))]
    return sum(vals), (sum(vals)/len(vals) if vals else 0), len(vals)
for k in ['raw','after_fdr','shock_src','cascades','kartograf','graph_nodes','gate_pass','track_money','track_prov','track_digest','sealed_money','sealed_prov','court_n','court_razbita','court_ustoyala','top3_out']:
    tot,avg,n = agg(k)
    print(f'{k:16s} sum={tot:7} avg={avg:8.2f} (n={n})')
