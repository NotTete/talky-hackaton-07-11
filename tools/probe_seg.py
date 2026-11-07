"""Why a segment's pairs are missed: per-line golden vs submitted."""
import gzip, csv, sys, collections, pathlib, polars as pl
R=dict(infer_schema_length=0,null_values=[""])
seg=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 3
build=sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R); t=pl.read_csv(base/"bank_transactions.csv.gz",**R)
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
doc={}
for f,c in (("ar_invoices.csv.gz","document_number"),("ap_invoices.csv.gz","supplier_invoice_number"),("credit_notes.csv.gz","document_number")):
    for r in pl.read_csv(base/f,**R).iter_rows(named=True): doc[r["item_id"]]=r[c]
sub=collections.defaultdict(dict)
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh):
        sub[row["bank_txn_id"]][row["item_id"] or "(cat)"]=(row["amount"],row["exception"],row["category"])
ids=[r["bank_txn_id"] for r in g.filter(pl.col("segment")==seg).iter_rows(named=True)]
seen=[]; 
for b in ids:
    if b not in seen: seen.append(b)
print("build:", build.name)
for bid in seen[:n]:
    r=T[bid]
    blob=" | ".join(str(r[k] or "") for k in ("text","ref1","ref2","remittance_info","counterparty_name","end_to_end_id") if r[k])
    gi={x["item_id"]:(x["amount"],x["exception"] or "") for x in g.filter(pl.col("bank_txn_id")==bid).iter_rows(named=True)}
    si=sub.get(bid,{})
    print("="*110); print(f"{bid} {r['amount']} :: {blob[:200]}")
    miss=[k for k in gi if k not in si]; extra=[k for k in si if k not in gi and k!="(cat)"]
    print(f"  golden {len(gi)} / sub {len(si)} | missing {len(miss)} extra {len(extra)}")
    for k in list(miss)[:8]: print(f"    MISS {k} {doc.get(k)} {gi[k]}")
    for k in list(extra)[:8]: print(f"    EXTRA {k} {doc.get(k)} {si[k]}")
    both=[k for k in gi if k in si][:4]
    for k in both:
        if gi[k][0]!=si[k][0] or gi[k][1]!=si[k][1]: print(f"    DIFF {k} golden={gi[k]} sub={si[k][:2]}")
    if "(cat)" in si: print(f"    CATEGORY {si['(cat)']}")
