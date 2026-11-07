"""Golden rows of one exception class, with what we submitted and the item's numbers."""
import gzip,csv,sys,collections,pathlib,polars as pl
R=dict(infer_schema_length=0,null_values=[""])
build=sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R); t=pl.read_csv(base/"bank_transactions.csv.gz",**R)
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
info={}
for f,c,pid in (("ar_invoices.csv.gz","document_number","customer_id"),("ap_invoices.csv.gz","supplier_invoice_number","supplier_id"),("credit_notes.csv.gz","document_number","party_id")):
    for r in pl.read_csv(base/f,**R).iter_rows(named=True):
        info[r["item_id"]]=(r[c], r.get("total_amount"), r.get("currency"), r.get("payment_terms"), r[pid])
cust={r["customer_id"]:r for r in pl.read_csv("data/masters/customers.csv",**R).iter_rows(named=True)}
sup={r["supplier_id"]:r for r in pl.read_csv("data/masters/suppliers.csv",**R).iter_rows(named=True)}
sub=collections.defaultdict(dict)
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh): sub[row["bank_txn_id"]][row["item_id"] or "(cat)"]=row
exc=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 10
rows=[r for r in g.iter_rows(named=True) if (r["exception"] or "")==exc]
print(f"{len(rows)} golden rows with {exc}")
for r in rows[:n]:
    bid=r["bank_txn_id"]; it=r["item_id"]; x=T[bid]
    d=info.get(it,("?","?","?","?","?"))
    party=cust.get(d[4]) or sup.get(d[4]) or {}
    s=sub[bid].get(it)
    blob=" | ".join(str(x[k] or "") for k in ("text","ref2","remittance_info") if x[k])[:100]
    print(f"  {bid} cash={x['amount']}{x['currency']} seg={r['segment']}")
    print(f"     item {it} doc={d[0]} total={d[1]}{d[2]} terms={d[3]} party={party.get('name')}/{party.get('country')}")
    print(f"     golden amt={r['amount']}  sub={(s['amount'], s['exception']) if s else 'MISSING'}  :: {blob}")
