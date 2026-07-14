# -*- coding: utf-8 -*-
"""H5 money-track hit rate + court pass-rate (read-only)."""
import json, pathlib, collections
ROOT = pathlib.Path("/home/oracle/oracle")
def read_jsonl(p):
    out=[]
    with open(p, encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if line: out.append(json.loads(line))
    return out
preds = read_jsonl(ROOT/"journal"/"predictions.jsonl")
outs  = read_jsonl(ROOT/"journal"/"outcomes.jsonl")
money = {p["hash"]:p for p in preds if p.get("kind")=="cascade_money" and p.get("hash")}
mout  = [o for o in outs if o.get("kind")=="cascade_money" and o.get("outcome") in (0,1)]
print("=== MONEY TRACK ===")
print("money predictions:", len(money), " resolved outcomes:", len(mout))
# sample keys
smp=next(iter(money.values()))
print("money pred keys:", sorted(smp.keys()))
print("money pred sample:", {k:smp[k] for k in ['hash','sealed_at','probability','asset','direction','threshold'] if k in smp})
print("money out keys:", sorted(mout[0].keys()))
joined=[(money[o["hash"]],o) for o in mout if o["hash"] in money]
N=len(joined); hits=sum(o["outcome"] for _,o in joined)
print(f"joined={N}  hit-rate={hits}/{N}={hits/N:.4f}" if N else "no joined")
if N:
    dp=[p.get("probability") for p,_ in joined]
    print(f"declared prob: mean={sum(dp)/len(dp):.4f} min={min(dp)} max={max(dp)}")
    print("sealed_at dates:", collections.Counter((p.get('sealed_at') or '')[:10] for p,_ in joined))
    print("per-prediction:")
    for p,o in sorted(joined,key=lambda x:x[0].get('sealed_at','')):
        print(f"  {p.get('sealed_at','')[:10]} {p.get('asset'):<12} P={p.get('probability')} dir={p.get('direction')} -> outcome={o['outcome']}")
# all money predictions sealed dates (incl unresolved)
print("ALL money pred sealed dates:", collections.Counter((p.get('sealed_at') or '')[:10] for p in money.values()))
