import polars as pl, sys, pathlib
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz")
t=pl.read_csv(base/"bank_transactions.csv.gz", infer_schema_length=5000)
T={r["bank_txn_id"]:r for r in t.iter_rows(named=True)}
for seg in sys.argv[1:]:
    ids=g.filter(pl.col("segment")==seg)["bank_txn_id"].unique().sort().to_list()[:2]
    print("#"*20, seg)
    for bid in ids:
        r=T[bid]
        blob=" | ".join(str(r[k]) for k in ("text","ref1","ref2","remittance_info","counterparty_name","end_to_end_id") if r[k])
        print(f"  {bid} {r['amount']:>12} {r['currency']} code={r['bank_code']}/{r['bank_subcode']} :: {blob[:180]}")
        sub=g.filter(pl.col("bank_txn_id")==bid)
        for x in sub.iter_rows(named=True)[:8] if False else sub.head(8).iter_rows(named=True):
            print(f"       -> {x['item_id']} {x['amount']} cat={x['category']} exc={x['exception']}")
        if len(sub)>8: print(f"       ... {len(sub)} rows")
