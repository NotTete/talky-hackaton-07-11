"""ar_short_dispute (and friends) lines we still cannot explain: what is the cash near?"""
import gzip,csv,collections,pathlib,sys,polars as pl
from bankrec.config import load_settings
from bankrec.data import Phase
from bankrec.alt.engine import Engine,_Context,_tol
R=dict(infer_schema_length=0,null_values=[""])
build=sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R)
gold=collections.defaultdict(list); gseg={}
for r in g.iter_rows(named=True):
    gseg[r["bank_txn_id"]]=r["segment"]
    if r["item_id"]: gold[r["bank_txn_id"]].append(r)
sub=collections.defaultdict(set)
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh):
        if row["item_id"]: sub[row["bank_txn_id"]].add(row["item_id"])
segs=set(sys.argv[1:]) or {"ar_short_dispute"}
bad=[b for b,rows in gold.items() if gseg[b] in segs and len(sub.get(b,()))*2 < len(rows)]
print(f"{len(bad)} lines of {segs} where we found less than half the pairs")
ph=Phase(load_settings(),"sample"); eng=Engine(ph, ph.items(), ph.masters)
txns={r["bank_txn_id"]:r for r in ph.bank_transactions().iter_rows(named=True)}
for b in bad[:10]:
    txn=txns[b]; ctx=_Context(txn,eng)
    cash=abs(ctx.cash)
    adv=eng.advices.by_token(ctx.op_tokens,cash,_tol(ctx.cash))
    pool=[]
    for pid in ctx.parties:
        for a in eng.advices._by_party.get(pid,()):
            pool.append((a.path, a.total_cents, a.sum_paid, len(a.lines), a.received_ymd))
    print(f"\n{b} cash={txn['amount']} golden={len(gold[b])} found={len(sub.get(b,()))}")
    print(f"   text: {(txn.get('text') or '')[:110]}")
    print(f"   parties={ctx.parties[:3]} adv_by_ref={adv.path if adv else None} optokens={ctx.op_tokens[:4]}")
    for x in pool[:4]: print(f"     cand advice {x}")
