"""Starter: load the data, naive match (exact amount + one of our document numbers in the bank text), write a valid submission.

    python starter/starter.py --data .. --phase live --out submission_live.csv.gz
    python starter/starter.py --data .. --phase train --out submission_train.csv.gz   # then: python starter/score_train.py ...

Standard library only. Everything that is not matched gets a no-item row; receipts -> UNIDENTIFIED, debits -> BANK_FEES
(both usually wrong: that is your job!).
"""
import argparse
import csv
import gzip
import os
import re
from collections import defaultdict


def rcsv(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def cents(s):
    return int(round(float(s) * 100)) if s else 0


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--phase", default="live", choices=["sample", "train", "live", "final"])
    ap.add_argument("--out", default="submission.csv.gz")
    a = ap.parse_args()
    phases = {"sample": ["sample"], "train": ["train"], "live": ["train", "live"], "final": ["train", "live", "final"]}[a.phase]
    # open items: everything issued up to the phase (a real solution would track balances over time)
    items, by_number = {}, defaultdict(list)
    for ph in phases:
        for name, numcol in (("ar_invoices", "document_number"), ("ap_invoices", "supplier_invoice_number"), ("credit_notes", "document_number")):
            p = os.path.join(a.data, ph, f"{name}.csv.gz")
            if not os.path.exists(p):
                continue
            for r in rcsv(p):
                items[r["item_id"]] = cents(r["total_amount"])
                by_number[norm(r[numcol])].append(r["item_id"])
    used = set()
    rows = []
    n_match = 0
    for t in rcsv(os.path.join(a.data, a.phase, "bank_transactions.csv.gz")):
        amt = cents(t["amount"])
        text = " ".join([t["text"], t["ref1"], t["ref2"], t["remittance_info"]]).upper()
        hit = None
        for tok in re.split(r"[\s|,;/]+", text):
            for iid in by_number.get(norm(tok), []) if len(tok) >= 5 else []:
                if iid not in used and items[iid] == abs(amt):
                    hit = iid
                    break
            if hit:
                break
        if hit:
            used.add(hit)
            n_match += 1
            rows.append([t["bank_txn_id"], hit, f"{abs(amt) / 100:.2f}", "", ""])
        elif amt > 0:
            rows.append([t["bank_txn_id"], "", "", "UNIDENTIFIED", "UNIDENTIFIED"])
        else:
            rows.append([t["bank_txn_id"], "", "", "BANK_FEES", ""])
    op = gzip.open if a.out.endswith(".gz") else open
    with op(a.out, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["bank_txn_id", "item_id", "amount", "category", "exception"])
        w.writerows(rows)
    print(f"{len(rows)} rows ({n_match} matched) -> {a.out}")


if __name__ == "__main__":
    main()
