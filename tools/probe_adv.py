"""For advice-driven segments: is the advice found, and do its lines resolve?"""
import gzip, csv, collections, pathlib, polars as pl, sys
from bankrec.config import load_settings
from bankrec.data import Phase
from bankrec.alt import advice as A, refs
from bankrec.alt.ledger import Ledger
R=dict(infer_schema_length=0,null_values=[""])
s=load_settings(); ph=Phase(s,"sample")
items=ph.items(); masters=ph.masters
led=Ledger(items,masters)
advs=A.load(ph)
idx=A.AdviceIndex(advs,led)
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R); t=pl.read_csv(base/"bank_transactions.csv.gz",**R)
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
segs=set(sys.argv[1:]) or {"ar_short_dispute"}
lines=collections.OrderedDict()
for r in g.iter_rows(named=True):
    if r["segment"] in segs: lines.setdefault(r["bank_txn_id"],[]).append(r)
print(f"{len(lines)} lines in {segs}; {len(advs)} advices")
found=0; nf=[]
for bid,rows in lines.items():
    r=T[bid]
    fields=(r.get("text"),r.get("ref1"),r.get("ref2"),r.get("remittance_info"),r.get("end_to_end_id"))
    toks=refs.operational(*(str(f or "") for f in fields))
    a=idx.by_token(toks)
    if a is None:
        owner=led.party_for_iban(r.get("counterparty_account") or "")
        party=owner[0] if owner else led.party_for_name(r.get("counterparty_name") or "")
        cash=abs(int(round(float(r["amount"])*100)))
        a=idx.by_party_total(party,cash,0,max(2,cash//200)) if party else None
    if a is None: nf.append((bid,len(rows),r.get("counterparty_name"),(r.get("text") or "")[:80]))
    else:
        found+=1
        # do the advice lines resolve?
        res=sum(1 for l in a.lines if led.by_doc(l.document))
        if res<len(a.lines) or len(a.lines)!=len(rows):
            print(f"  {bid}: golden {len(rows)} rows, advice {a.path} has {len(a.lines)} lines, {res} resolve")
print(f"advice found for {found}/{len(lines)}")
for x in nf[:10]: print("  NOADVICE", x)
