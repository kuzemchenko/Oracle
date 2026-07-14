#!/usr/bin/env python3
"""Read-only analysis of predictions.jsonl + outcomes.jsonl for §11 gate pace.
open() on read is invisible to the guard hook (no shell redirection)."""
import json, collections, datetime, os

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PRED = os.path.join(BASE, 'journal', 'predictions.jsonl')
OUT = os.path.join(BASE, 'journal', 'outcomes.jsonl')

def load(path):
    rows = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows

preds = load(PRED)
outs = load(OUT)
print('TOTAL predictions:', len(preds))
print('TOTAL outcomes:', len(outs))

# keys present
allkeys = collections.Counter()
for d in preds:
    for k in d.keys():
        allkeys[k] += 1
print('\n=== prediction keys (freq) ===')
for k, v in allkeys.most_common():
    print(f'  {k}: {v}')

# kind distribution
kc = collections.Counter(d.get('kind', '<none>') for d in preds)
print('\n=== kind distribution ===')
for k, v in kc.most_common():
    print(f'  {k}: {v}')

# track field maybe
tc = collections.Counter(d.get('track', '<none>') for d in preds)
print('\n=== track distribution ===')
for k, v in tc.most_common():
    print(f'  {k}: {v}')

# find sealed_at field name
ts_fields = [k for k in allkeys if any(x in k.lower() for x in ('seal', 'ts', 'time', 'date', 'created'))]
print('\n=== timestamp-ish fields ===', ts_fields)

def parse_ts(d):
    for f in ('sealed_at', 'ts', 'timestamp', 'created_at', 'sealed_ts', 'seal_ts', 'time'):
        v = d.get(f)
        if v:
            for fmt in (None,):
                try:
                    if isinstance(v, (int, float)):
                        return datetime.datetime.utcfromtimestamp(v).date()
                    s = str(v).replace('Z', '').split('.')[0]
                    return datetime.datetime.fromisoformat(s).date()
                except Exception:
                    try:
                        return datetime.datetime.strptime(str(v)[:15], '%Y%m%dT%H%M%S').date()
                    except Exception:
                        pass
    return None

# money-track identification: kind money OR funnel_forward
MONEY_KINDS = set()
for d in preds:
    k = str(d.get('kind', ''))
    if 'money' in k.lower() or 'funnel' in k.lower():
        MONEY_KINDS.add(k)
print('\n=== detected money-ish kinds ===', MONEY_KINDS)

# by kind, weekly sealed counts (last 6 weeks)
today = datetime.date(2026, 7, 14)
def weekkey(dt):
    if not dt:
        return None
    delta = (today - dt).days
    return delta // 7  # 0 = last 7 days, 1 = prior week...

print('\n=== weekly sealed counts by kind (week 0 = last 7d ending 2026-07-14) ===')
kind_week = collections.defaultdict(lambda: collections.Counter())
undated = collections.Counter()
for d in preds:
    dt = parse_ts(d)
    k = d.get('kind', '<none>')
    if dt is None:
        undated[k] += 1
        continue
    wk = weekkey(dt)
    kind_week[k][wk] += 1
for k in sorted(kind_week):
    wks = kind_week[k]
    line = ' '.join(f'w{w}={wks[w]}' for w in sorted(wks))
    print(f'  {k}: {line}  (undated={undated[k]})')

# overall date range
dts = [parse_ts(d) for d in preds]
dts = [x for x in dts if x]
if dts:
    print('\nprediction date range:', min(dts), '..', max(dts), ' dated:', len(dts), '/', len(preds))

# outcomes analysis
print('\n=== outcomes ===')
ok = collections.Counter()
for d in outs:
    for k in d.keys():
        ok[k] += 1
print('outcome keys:', dict(ok.most_common()))
okind = collections.Counter(d.get('kind', d.get('track', '<none>')) for d in outs)
print('outcome kind/track:', dict(okind))

# money outcomes resolved
def is_money(d):
    k = str(d.get('kind', '')) + str(d.get('track', ''))
    return 'money' in k.lower() or 'funnel' in k.lower()
money_out = [d for d in outs if is_money(d)]
print('money-ish outcomes resolved:', len(money_out))
