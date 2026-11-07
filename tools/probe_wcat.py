"""Lines where golden has items but we emitted a category: what evidence did we have?"""
import gzip, csv, collections, pathlib, sys, polars as pl, random
R=dict(infer_schema_length=0,null_values=[""])
build=sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R); t=pl.read_csv(base/"bank_transactions.csv.gz",**R)
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
doc={}
for f,c in (("ar_invoices.csv.gz","document_number"),("ap_invoices.csv.gz","supplier_invoice_number"),("credit_notes.csv.gz","document_number")):
    for r in pl.read_csv(base/f,**R).iter_rows(named=True): doc[r["item_id"]]=r[c]
gold=collections.defaultdict(list); gseg={}
for r in g.iter_rows(named=True):
    gseg[r["bank_txn_id"]]=r["segment"]
    if r["item_id"]: gold[r["bank_txn_id"]].append(r)
sub=collections.defaultdict(dict); cat={}
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh):
        if row["item_id"]: sub[row["bank_txn_id"]][row["item_id"]]=row
        else: cat[row["bank_txn_id"]]=row["category"]
seg=sys.argv[1] if len(sys.argv)>1 else None
bad=[b for b in gold if b in cat and (seg is None or gseg[b]==seg)]
print(f"{len(bad)} lines with golden items but a category row", f"(segment {seg})" if seg else "")
print(collections.Counter(gseg[b] for b in bad).most_common(10))
print(collections.Counter(cat[b] for b in bad).most_common(8))
random.seed(3); random.shuffle(bad)
for b in bad[:int(sys.argv[2]) if len(sys.argv)>2 else 8]:
    r=T[b]
    blob=" | ".join(str(r[k] or "") for k in ("text","ref1","ref2","remittance_info","counterparty_name") if r[k])
    print(f"\n{b} {r['amount']} {gseg[b]} -> {cat[b]}")
    print(f"   {blob[:170]}")
    print(f"   golden: {[(doc.get(x['item_id']), x['amount'], x['exception']) for x in gold[b]][:6]}")
