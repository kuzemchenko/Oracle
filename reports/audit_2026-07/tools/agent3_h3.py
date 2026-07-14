# -*- coding: utf-8 -*-
"""H3 calibration: independent reproduction from sealed journals (read-only)."""
import json, pathlib, collections
ROOT = pathlib.Path("/home/oracle/oracle")
def read_jsonl(p):
    out=[]
    with open(p, encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if line: out.append(json.loads(line))
    return out
preds = {r["hash"]: r for r in read_jsonl(ROOT/"journal"/"predictions.jsonl")
         if r.get("kind")=="calibration" and r.get("hash")}
outs = [o for o in read_jsonl(ROOT/"journal"/"outcomes.jsonl")
        if o.get("kind")=="calibration" and o.get("outcome") in (0,1)]
joined = [(preds[o["hash"]], o) for o in outs if o["hash"] in preds]
N = len(joined)
hits = sum(o["outcome"] for _,o in joined)
print(f"N calib predictions={len(preds)}  resolved outcomes={len(outs)}  joined={N}  unmatched={len(outs)-N}")
print(f"OVERALL hit-rate = {hits}/{N} = {hits/N:.4f}")
# Brier using PREDICTION probability (true declared per-threshold prob)
brier_pred = sum((p["probability"]-o["outcome"])**2 for p,o in joined)/N
# Brier using OUTCOME-record probability (the 0.5-everywhere SYNC value)
brier_out = sum((o["probability"]-o["outcome"])**2 for _,o in joined)/N
print(f"Brier (by prediction.probability)={brier_pred:.4f}  Brier (by outcome.probability={{0.5}})={brier_out:.4f}  coin=0.25")
print()
# by k_sigma
print("k_sigma | n | declared_prob(pred) | hit-rate")
by_k = collections.defaultdict(list)
for p,o in joined:
    by_k[p["k_sigma"]].append((p["probability"], o["outcome"]))
for k in sorted(by_k):
    rows=by_k[k]
    n=len(rows); hr=sum(o for _,o in rows)/n
    dp=rows[0][0]
    print(f"{k:+.1f}   | {n} | {dp} | {hr:.4f}")
print()
# by probability bucket (prediction.probability)
print("prob bucket | n | mean_declared | hit-rate")
def bucket(x):
    return round(x,4)
by_p = collections.defaultdict(list)
for p,o in joined:
    by_p[bucket(p["probability"])].append(o["outcome"])
for pb in sorted(by_p):
    rows=by_p[pb]; n=len(rows); hr=sum(rows)/n
    print(f"{pb} | {n} | {pb} | {hr:.4f}")
print()
# by run_id (batch)
print("batch | n | hit-rate")
by_b=collections.defaultdict(list)
for p,o in joined:
    by_b[p.get("run_id")].append(o["outcome"])
for b in sorted(by_b):
    rows=by_b[b]; print(f"{b} | {len(rows)} | {sum(rows)/len(rows):.4f}")
# also: outcome.probability distribution (what SYNC used)
print()
print("outcome.probability distribution:", collections.Counter(round(o['probability'],4) for _,o in joined))
