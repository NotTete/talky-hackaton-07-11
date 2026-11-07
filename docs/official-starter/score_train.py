"""Score a TRAIN (or sample) submission locally with the official scorer.

    python starter/score_train.py submission_train.csv.gz [--data ..] [--sample]
"""
import argparse
import csv
import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bankrec_score as S  # noqa: E402

EXC = ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING", "SHORT_DISPUTE", "DUPLICATE_PAYMENT",
       "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("submission")
    ap.add_argument("--data", default=os.path.join(HERE, ".."))
    ap.add_argument("--sample", action="store_true", help="score against sample/ instead of train/")
    a = ap.parse_args()
    folder = os.path.join(a.data, "sample" if a.sample else "train")
    cats = [r["category"] for r in csv.DictReader(open(os.path.join(a.data, "masters", "categories.csv"), encoding="utf-8"))]
    with tempfile.NamedTemporaryFile("wb", suffix=".csv.gz", delete=False) as tmp:
        pass
    with gzip.open(os.path.join(folder, "bank_transactions.csv.gz"), "rt", encoding="utf-8") as fh, gzip.open(tmp.name, "wt") as out:
        out.write("bank_txn_id\n")
        for r in csv.DictReader(fh):
            out.write(r["bank_txn_id"] + "\n")
    g = S.load_golden(os.path.join(folder, "golden_allocations.csv.gz"), tmp.name, EXC, cats)
    os.unlink(tmp.name)
    res = S.score_submission(g, open(a.submission, "rb").read(), a.submission)
    print(json.dumps({k: res[k] for k in ("ok", "score", "metrics", "errors")}, indent=1))
    for k, v in sorted(res["segments"].items(), key=lambda x: -x[1]["n"])[:40]:
        print(f"  {k:32s} n={v['n']:7d} acc={v['accuracy']:.3f}")


if __name__ == "__main__":
    main()
