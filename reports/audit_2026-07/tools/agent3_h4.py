# -*- coding: utf-8 -*-
"""H4 amplitude/R²/tier distribution (read-only)."""
import json, glob, os, collections, pathlib, statistics
ROOT=pathlib.Path('/home/oracle/oracle')
def rj(p): return [json.loads(l) for l in open(p,encoding='utf-8') if l.strip()]

# --- R2 from sealed cascade predictions ---
preds=rj(ROOT/'journal'/'predictions.jsonl')
r2_by_kind=collections.defaultdict(list)
for p in preds:
    k=p.get('kind')
    if k in ('cascade_money','cascade_provisional','cascade_forward','edge_forward'):
        r=p.get('reliability_r2')
        if r is not None: r2_by_kind[k].append(r)
print("=== reliability_r2 from sealed predictions ===")
allr=[]
for k,v in r2_by_kind.items():
    allr+=v
    v2=sorted(v)
    print(f"{k}: n={len(v)} min={min(v):.4f} median={statistics.median(v):.4f} mean={statistics.mean(v):.4f} max={max(v):.4f}")
print(f"ALL cascade r2: n={len(allr)} median={statistics.median(allr):.4f} mean={statistics.mean(allr):.4f}")
# histogram
bins=[0,0.05,0.1,0.2,0.3,0.5,1.01]
h=collections.Counter()
for r in allr:
    for i in range(len(bins)-1):
        if bins[i]<=r<bins[i+1]: h[f"[{bins[i]},{bins[i+1]})"]+=1;break
print("r2 histogram:", dict(sorted(h.items())))

# --- tiers from граф_отбор топ_k across ef runs ---
print("\n=== ярусы (tiers) across ef граф_отбор.топ_k ===")
tier_lowest=collections.Counter()
money_tiers=collections.Counter()
prov_tiers=collections.Counter()
def low(ts):
    rank={'A':0,'B':1,'C':2}
    present=[t for t in ts if t in rank]
    return max(present,key=lambda t:rank[t]) if present else None
for f in sorted(glob.glob('journal/funnel_logs/ef_*.json')):
    if '__' in os.path.basename(f): continue
    d=json.load(open(f)); go=d.get('граф_отбор') or {}
    for row in go.get('топ_k') or []:
        lt=low(row.get('ярусы') or [])
        if lt: tier_lowest[lt]+=1
    for row in go.get('money_трек') or []:
        lt=low(row.get('ярусы') or []);
        if lt: money_tiers[lt]+=1
    for row in go.get('провизорный_трек') or []:
        lt=low(row.get('ярусы') or [])
        if lt: prov_tiers[lt]+=1
print("lowest-tier of candidates (топ_k):", dict(tier_lowest))
print("money-track lowest-tier:", dict(money_tiers))
print("провизорный-track lowest-tier:", dict(prov_tiers))
tot=sum(tier_lowest.values())
A=tier_lowest.get('A',0)
print(f"\nCandidates tier-A (money-eligible)={A}/{tot}={A/tot:.1%}; tier B/C (cut from money -> research-only)={tot-A}/{tot}={(tot-A)/tot:.1%}")
