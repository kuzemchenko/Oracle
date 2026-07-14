# -*- coding: utf-8 -*-
"""Найти money-печати (kind=cascade_money) с R²=1.0 / вырожденным фитом. Read-only predictions.jsonl.
Печатает, в каком поле живёт r², и список id для аннотации Этапа 1."""
import json, pathlib, re
P = pathlib.Path("/root/oracle/journal/predictions.jsonl")
rows = [json.loads(l) for l in P.read_text(encoding="utf-8").splitlines() if l.strip()]
money = [r for r in rows if r.get("kind") == "cascade_money"]
print(f"всего cascade_money: {len(money)}")
# где может лежать r2 — ищем ключ r2/r²/reliability в записи и вложенных
def find_r2(obj, path=""):
    hits=[]
    if isinstance(obj, dict):
        for k,v in obj.items():
            if re.search(r'r2|r²|reliab|надёж', str(k), re.I) and isinstance(v,(int,float)):
                hits.append((path+"."+k, v))
            hits += find_r2(v, path+"."+k)
    elif isinstance(obj, list):
        for i,v in enumerate(obj[:6]):
            hits += find_r2(v, f"{path}[{i}]")
    return hits
degen_ids=[]
seen_fields=set()
for r in money:
    hits = find_r2(r)
    r2_1 = [(p,v) for p,v in hits if abs(v-1.0)<1e-6]
    for p,_ in hits: seen_fields.add(p.split('[')[0])
    if r2_1:
        rid = r.get("rec_hash") or r.get("hash") or r.get("id") or r.get("run_id")
        degen_ids.append((rid, r.get("asset"), r.get("sealed_at"), [p for p,_ in r2_1]))
print("поля с r2-подобными:", sorted(seen_fields)[:12])
print(f"\nmoney-печатей с r²=1.0: {len(degen_ids)}")
for rid,asset,ts,ps in degen_ids:
    print(f"  {ts}  {asset:<12} {str(rid)[:16]}  поля={ps}")
