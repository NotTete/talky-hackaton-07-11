"""Per-segment pair-level diagnosis of a build against the golden."""
import gzip, sys, csv, collections, pathlib, polars as pl
R=dict(infer_schema_length=0,null_values=[""])
phase=sys.argv[2] if len(sys.argv)>2 else "sample"
base=pathlib.Path("data")/phase
g=pl.read_csv(base/"golden_allocations.csv.gz",**R)
build=pathlib.Path(sys.argv[1])
if not build.exists():
    build=sorted(pathlib.Path("builds").glob("*.csv.gz"))[-1]
gold={}; gseg={}; gcat={}
for r in g.iter_rows(named=True):
    bid=r["bank_txn_id"]; gseg[bid]=r["segment"]
    if r["item_id"]: gold.setdefault(bid,{})[r["item_id"]]=(float(r["amount"]), r["exception"] or "")
    else: gcat[bid]=(r["category"] or "", r["exception"] or "")
sub=collections.defaultdict(dict); subcat={}
with gzip.open(build,"rt") as fh:
    for row in csv.DictReader(fh):
        if row["item_id"]: sub[row["bank_txn_id"]][row["item_id"]]=(float(row["amount"]), row["exception"])
        else: subcat[row["bank_txn_id"]]=(row["category"], row["exception"])
def tol(a): return max(0.01, 0.005*abs(a))
stat=collections.defaultdict(lambda: dict(n=0, ok=0, tp=0, fp=0, fn=0, amt=0, exc=0, wrongcat=0, miss=0, extra=0))
for bid, seg in gseg.items():
    s=stat[seg]; s["n"]+=1
    gi=gold.get(bid,{}); si=sub.get(bid,{})
    if gi:
        if bid in subcat: s["wrongcat"]+=1
        tp=0; allok=True
        for item,(ga,ge) in gi.items():
            if item in si:
                sa,se=si[item]
                if abs(sa-ga)<=tol(ga):
                    tp+=1; s["tp"]+=1
                    if se!=ge: s["exc"]+=1; allok=False
                else: s["amt"]+=1; s["fp"]+=1; allok=False
            else: s["fn"]+=1; s["miss"]+=1; allok=False
        for item in si:
            if item not in gi: s["fp"]+=1; s["extra"]+=1; allok=False
        if allok and len(si)==len(gi) and bid not in subcat: s["ok"]+=1
    else:
        if bid in subcat and not si:
            c,e=subcat[bid]; gc,ge=gcat.get(bid,("",""))
            if c==gc and e==ge: s["ok"]+=1
            else: s["wrongcat"]+=1
        else: s["extra"]+=len(si)
rows=sorted(stat.items(), key=lambda kv: -(kv[1]["fn"]+kv[1]["fp"]))
print(f"{'segment':32}{'lines':>6}{'ok':>6}{'acc':>7}{'TP':>7}{'FP':>7}{'FN':>7}{'badamt':>7}{'badexc':>7}{'miss':>7}{'extra':>7}{'wcat':>6}")
for seg,s in rows:
    print(f"{seg:32}{s['n']:>6}{s['ok']:>6}{s['ok']/max(s['n'],1):>7.2f}{s['tp']:>7}{s['fp']:>7}{s['fn']:>7}{s['amt']:>7}{s['exc']:>7}{s['miss']:>7}{s['extra']:>7}{s['wrongcat']:>6}")
tot=collections.Counter()
for s in stat.values():
    for k,v in s.items(): tot[k]+=v
print(f"{'TOTAL':32}{tot['n']:>6}{tot['ok']:>6}{tot['ok']/max(tot['n'],1):>7.2f}{tot['tp']:>7}{tot['fp']:>7}{tot['fn']:>7}{tot['amt']:>7}{tot['exc']:>7}{tot['miss']:>7}{tot['extra']:>7}{tot['wrongcat']:>6}")

# ---- exception confusion (submitted vs golden), on TP rows only
conf=collections.Counter()
for bid,gi in gold.items():
    si=sub.get(bid,{})
    for item,(ga,ge) in gi.items():
        if item in si:
            sa,se=si[item]
            if se!=ge: conf[(ge,se,gseg.get(bid,""))]+=1
print("\ngolden_exc -> submitted_exc  (count, top segment)")
agg=collections.Counter()
for (ge,se,seg),v in conf.items(): agg[(ge,se)]+=v
for (ge,se),v in agg.most_common(25):
    segs=collections.Counter({s:c for (g2,s2,s),c in conf.items() if g2==ge and s2==se})
    print(f"  {ge or '(empty)':20} -> {se or '(empty)':20} {v:>6}   {segs.most_common(2)}")
