import polars as pl, sys, pathlib
base=pathlib.Path("data/sample")
g=pl.read_csv(base/"golden_allocations.csv.gz")
t=pl.read_csv(base/"bank_transactions.csv.gz", infer_schema_length=5000)
seg=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 3
ids=g.filter(pl.col("segment")==seg)["bank_txn_id"].unique().sort().to_list()[:n]
for bid in ids:
    row=t.filter(pl.col("bank_txn_id")==bid).to_dicts()[0]
    print("="*100)
    for k,v in row.items():
        if v is not None and v!="": print(f"  {k:20} {v}")
    sub=g.filter(pl.col("bank_txn_id")==bid)
    print(sub.select(["item_id","amount","category","exception"]).to_pandas().to_string())
