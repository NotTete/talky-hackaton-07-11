from bankrec.config import load_settings
from bankrec.data import Phase
from bankrec.alt.ops import Operations
from bankrec.alt import refs
import collections, polars as pl
s=load_settings(); ph=Phase(s,"sample")
ops=Operations(ph)
t=ph.bank_transactions()
hits=collections.defaultdict(list)
for r in t.iter_rows(named=True):
    fields=(r.get("text"),r.get("ref1"),r.get("ref2"),r.get("remittance_info"),r.get("end_to_end_id"))
    op=ops.by_token(refs.operational(*(str(f or "") for f in fields)))
    if op is not None:
        hits[id(op)].append((r["bank_txn_id"], r["amount"], op.kind, op.total_cents, (r.get("text") or "")[:60]))
multi={k:v for k,v in hits.items() if len(v)>1}
print("ops claimed by >1 bank line:", len(multi), "of", len(hits))
for k,v in list(multi.items())[:6]:
    print("  ---", v[0][2], "total", v[0][3])
    for x in v[:5]: print("     ", x[0], x[1], x[4])
print()
# how often does the op total disagree with the cash?
bad=0; tot=0
for k,v in hits.items():
    for bid,amt,kind,total,txt in v:
        tot+=1
        if abs(abs(int(round(float(amt)*100)))-total) > max(2,int(0.01*total)): bad+=1
print(f"op total != line amount: {bad}/{tot}")
