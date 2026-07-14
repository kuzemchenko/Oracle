# -*- coding: utf-8 -*-
"""H5 court pass-rate before/after ~29.06 from funnel_logs граф_отбор (read-only)."""
import json, glob, os, collections, re
runs=[]
balls=[]
rubric_fail=collections.Counter()
for f in sorted(glob.glob('journal/funnel_logs/*.json')):
    b=os.path.basename(f)
    if '__' in b: continue
    try: d=json.load(open(f))
    except Exception: continue
    if not isinstance(d,dict): continue
    go=d.get('граф_отбор') or {}
    treki=go.get('треки') or {}
    seal=go.get('запечатано') or {}
    court=go.get('суд_money')
    m=re.search(r'(\d{8})T', b); date=m.group(1) if m else '????????'
    n_cand=treki.get('money')
    n_seal=seal.get('money')
    n_demot=seal.get('демотировано_судом', 0)
    # only runs where money track exists and court had something to decide
    if n_cand is None and not court: continue
    runs.append((date,b,n_cand,n_seal,n_demot,court))
    if isinstance(court,dict):
        for a,v in court.items():
            if isinstance(v,dict):
                balls.append((date,a,v.get('балл'),v.get('исход')))
                # crude rubric-question attribution from filled reason fields
                if v.get('кто_против'): rubric_fail['кто_против(кто продаёт и почему неправ)']+=1
                if v.get('почему_возможность'): rubric_fail['почему_возможность(неотыгранность)']+=1

print("=== PER-RUN MONEY COURT ===")
print("date     | cand | sealed(passed) | demoted(broken) | file")
for date,b,nc,ns,nd,court in runs:
    if nc or nd or (court):
        print(f"{date} | {nc} | {ns} | {nd} | {b}")

cut='20260629'
def agg(pred):
    cand=seal=demot=0
    for date,b,nc,ns,nd,court in runs:
        if not pred(date): continue
        cand+=nc or 0; seal+=ns or 0; demot+=nd or 0
    return cand,seal,demot
for label,pred in [("BEFORE 29.06 (<20260629)", lambda d:d<cut),
                   ("ON/AFTER 29.06 (>=20260629)", lambda d:d>=cut)]:
    cand,seal,demot=agg(pred)
    pr = seal/cand if cand else float('nan')
    print(f"\n{label}: money-cand={cand} sealed(passed)={seal} demoted(broken)={demot} pass-rate={pr:.2%}")

print("\n=== RUBRIC BALL DISTRIBUTION (dict verdicts, порог=3.0) ===")
bvals=[b for _,_,b,_ in balls if b is not None]
print("n scored verdicts:", len(bvals))
if bvals:
    print("balls:", sorted(bvals))
    print("all below порог 3.0:", all(b<3.0 for b in bvals), " max=", max(bvals))
print("outcome tally of scored verdicts:", collections.Counter(o for *_,o in balls))
print("\n=== RUBRIC FAIL ATTRIBUTION (reason fields populated) ===")
for k,n in rubric_fail.most_common(): print(f"  {k}: {n}")
