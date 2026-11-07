"""Per-class exception precision/recall exactly as the scorer computes them."""
import gzip, csv, collections, pathlib, sys, polars as pl
R=dict(infer_schema_length=0,null_values=[""])
build=pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz",**R)
EXC=["PARTIAL","OVERPAYMENT","SHORT_DISCOUNT","SHORT_BANK_CHARGES","SHORT_WITHHOLDING",
     "SHORT_DISPUTE","DUPLICATE_PAYMENT","UNIDENTIFIED","RETURNED","FX_DIFFERENCE"]
gex={}; gcat={}
for r in g.iter_rows(named=True):
    if r["item_id"]: gex[(r["bank_txn_id"],r["item_id"])]=r["exception"] or ""
    else: gcat[r["bank_txn_id"]]=r["exception"] or ""
gold=collections.Counter(v for v in gex.values() if v)+collections.Counter(v for v in gcat.values() if v)
tp=collections.Counter(); fp=collections.Counter()
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh):
        e=row["exception"]
        if not e: continue
        if row["item_id"]:
            ge=gex.get((row["bank_txn_id"],row["item_id"]))
        else:
            ge=gcat.get(row["bank_txn_id"]) if row["bank_txn_id"] not in {k[0] for k in ()} else None
            # scorer: no-item row scores only when golden also has no items
            ge=gcat.get(row["bank_txn_id"])
        if e==ge: tp[e]+=1
        else: fp[e]+=1
f1s=[]
print(f"{'exception':20}{'gold':>7}{'tp':>7}{'fp':>7}{'P':>7}{'R':>7}{'F1':>7}")
for c in EXC:
    if not gold[c]: continue
    fn=gold[c]-tp[c]
    p=tp[c]/(tp[c]+fp[c]) if tp[c]+fp[c] else 0.0
    r=tp[c]/(tp[c]+fn) if tp[c]+fn else 0.0
    f1=2*p*r/(p+r) if p+r else 0.0
    f1s.append(f1)
    print(f"{c:20}{gold[c]:>7}{tp[c]:>7}{fp[c]:>7}{p:>7.3f}{r:>7.3f}{f1:>7.3f}")
print(f"{'macro':20}{'':>21}{'':>14}{sum(f1s)/len(f1s):>7.3f}   (each class is worth 2.0 score points)")
