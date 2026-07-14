# -*- coding: utf-8 -*-
"""Read-only probe of journal record shapes (no writes to sealed journals)."""
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
print("TOTAL preds", len(preds), "outcomes", len(outs))
print("pred kinds:", collections.Counter(p.get("kind") for p in preds))
print("outcome kinds:", collections.Counter(o.get("kind") for o in outs))
# sample calibration prediction
for p in preds:
    if p.get("kind")=="calibration":
        print("CALIB PRED KEYS:", sorted(p.keys()))
        print("sample:", {k:p[k] for k in list(p.keys())[:30]})
        break
for o in outs:
    if o.get("kind")=="calibration" and o.get("outcome") in (0,1):
        print("CALIB OUT KEYS:", sorted(o.keys()))
        print("sample:", o)
        break
