import polars as pl, pathlib, re, sys, collections
base=pathlib.Path("data/sample"); R=dict(infer_schema_length=0,null_values=[""])
g=pl.read_csv(base/"golden_allocations.csv.gz",**R); t=pl.read_csv(base/"bank_transactions.csv.gz",**R)
doc={}; po={}
ar=pl.read_csv(base/"ar_invoices.csv.gz",**R); ap=pl.read_csv(base/"ap_invoices.csv.gz",**R); cn=pl.read_csv(base/"credit_notes.csv.gz",**R)
for r in ar.iter_rows(named=True): doc[r["item_id"]]=r["document_number"]; po[r["item_id"]]=r["customer_po"]
for r in ap.iter_rows(named=True): doc[r["item_id"]]=r["supplier_invoice_number"]
for r in cn.iter_rows(named=True): doc[r["item_id"]]=r["document_number"]
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
seg=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 12
ids=g.filter(pl.col("segment")==seg)["bank_txn_id"].unique().sort().to_list()
import random; random.seed(1); random.shuffle(ids)
for bid in ids[:n]:
    r=T[bid]; blob=" | ".join(str(r[k] or "") for k in ("text","ref1","ref2","remittance_info") if r[k])
    sub=g.filter(pl.col("bank_txn_id")==bid)
    docs=[(doc.get(x,'?'),po.get(x,'')) for x in sub["item_id"].to_list() if x]
    print(f"{bid} {r['amount']:>11} :: {blob[:140]}")
    print(f"      golden docs: {docs[:6]}  (sum={sum(float(a) for a in sub['amount'].to_list() if a):.2f})")
