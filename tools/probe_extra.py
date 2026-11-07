"""Items we emit that the golden does not have, for one segment."""
import gzip,csv,collections,pathlib,sys,polars as pl,random
R=dict(infer_schema_length=0,null_values=[""])
build=pathlib.Path(sys.argv[2]) if len(sys.argv)>2 else sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R); t=pl.read_csv(base/"bank_transactions.csv.gz",**R)
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
doc={}
for f,c in (("ar_invoices.csv.gz","document_number"),("ap_invoices.csv.gz","supplier_invoice_number"),("credit_notes.csv.gz","document_number")):
    for r in pl.read_csv(base/f,**R).iter_rows(named=True): doc[r["item_id"]]=r[c]
gold=collections.defaultdict(dict); gseg={}
for r in g.iter_rows(named=True):
    gseg[r["bank_txn_id"]]=r["segment"]
    if r["item_id"]: gold[r["bank_txn_id"]][r["item_id"]]=r["amount"]
sub=collections.defaultdict(dict)
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh):
        if row["item_id"]: sub[row["bank_txn_id"]][row["item_id"]]=(row["amount"],row["exception"])
seg=sys.argv[1]
lines=[b for b in sub if gseg.get(b)==seg and any(i not in gold[b] for i in sub[b])]
print(f"{len(lines)} lines of {seg} with extra items (build {build.name})")
random.seed(5); random.shuffle(lines)
for b in lines[:8]:
    x=T[b]; extra=[i for i in sub[b] if i not in gold[b]]
    print(f"\n{b} cash={x['amount']} golden={len(gold[b])} sub={len(sub[b])} extra={len(extra)}")
    print(f"   {(x.get('text') or '')[:120]}")
    for i in extra[:5]: print(f"   EXTRA {i} {doc.get(i)} {sub[b][i]}")
    for i in list(gold[b])[:3]: print(f"   GOLD  {i} {doc.get(i)} {gold[b][i]}{'  (found)' if i in sub[b] else ''}")
