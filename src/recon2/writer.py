"""Submission writer + format guards (DESIGN §4 'Writer guards', findings_spec §2).

`guard()` is called by the engine BEFORE book.apply so a demoted line never mutates the
book; `write()` re-checks every decision as a last safety net (a violating line is
demoted to its fallback category, never written invalid).
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import re
from pathlib import Path

from recon2.model import Alloc, Decision

ITEM_RE = re.compile(r"^(ARI|ARC|API|APC)-\d{4}-\d{7}$")
EXCEPTIONS = {
    "PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING",
    "SHORT_DISPUTE", "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE",
}
CATEGORIES = {
    "BANK_FEES", "INTEREST_INCOME", "INTEREST_EXPENSE", "PAYROLL", "SOCIAL_SECURITY",
    "TAX_PAYMENT", "TAX_REFUND", "LOAN_DRAWDOWN", "LOAN_REPAYMENT", "LEASING", "FX_DEAL",
    "CASH_POOL", "INTERCOMPANY", "INTERNAL_TRANSFER", "CUSTOMER_ADVANCE", "CUSTOMER_REFUND",
    "SUPPLIER_REFUND", "CARD_EXPENSES", "INSURANCE", "BANK_REVERSAL", "UNIDENTIFIED",
}
SOLO = {"OVERPAYMENT", "SHORT_BANK_CHARGES", "DUPLICATE_PAYMENT"}
HEADER = ["bank_txn_id", "item_id", "amount", "category", "exception"]


def fmt_cents(c: int) -> str:
    c = int(c)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100}.{c % 100:02d}"


def guard(allocs: list[Alloc], item_ids: list[str]) -> str | None:
    """None if the rows are writable, else a short reason. item_ids[i] = Book.items[i].item_id."""
    if not allocs:
        return None
    seen = set()
    for a in allocs:
        iid = item_ids[a.item]
        if not ITEM_RE.match(iid):
            return f"bad item_id {iid}"
        if type(a.amount) is not int or a.amount <= 0:
            return f"amount {a.amount!r} not >0 int"
        if a.exc and a.exc not in EXCEPTIONS:
            return f"unknown exc {a.exc}"
        if a.exc == "UNIDENTIFIED":
            return "UNIDENTIFIED on item row"
        if iid in seen:
            return f"duplicate pair {iid}"
        seen.add(iid)
    if len(allocs) > 1 and any(a.exc in SOLO for a in allocs):
        return "solo exception on multi-item line"
    return None


def write(decisions: list[Decision], item_ids: list[str], path: Path, fallback: dict[str, str]) -> dict:
    """Write csv.gz atomically. fallback: txn -> category used if a line must be demoted here."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    n_rows = demoted = 0
    seen_txn = set()
    for d in decisions:
        if d.txn in seen_txn:
            raise ValueError(f"line {d.txn} decided twice")
        seen_txn.add(d.txn)
        allocs, cat, exc = d.allocs, d.category, d.exc
        reason = guard(allocs, item_ids)
        if allocs and reason:
            demoted += 1
            d.trace.setdefault("caps", []).append("GUARD_DEMOTED:" + reason)
            allocs, cat = [], fallback.get(d.txn) or "UNIDENTIFIED"
            exc = "UNIDENTIFIED" if cat == "UNIDENTIFIED" else ""
            d.allocs, d.category, d.exc = allocs, cat, exc
        if allocs:
            for a in allocs:
                w.writerow([d.txn, item_ids[a.item], fmt_cents(a.amount), "", a.exc])
                n_rows += 1
        else:
            if cat not in CATEGORIES:
                cat = fallback.get(d.txn) or "UNIDENTIFIED"
                d.category = cat
            if exc not in ("", "UNIDENTIFIED"):
                exc = ""
            w.writerow([d.txn, "", "", cat, exc])
            n_rows += 1
    data = buf.getvalue().encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
            gz.write(data)
    os.replace(tmp, path)
    return {"rows": n_rows, "lines": len(decisions), "demoted": demoted, "bytes": len(data)}
