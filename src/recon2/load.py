"""Phase loader: polars for IO (all columns as strings), converted once to model records."""

from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from recon2.fx import FX
from recon2.model import DATA, Item, Line, day
from recon2.money import cents
from recon2.textviews import build

PHASES = ("sample", "live", "final", "train", "final_sim")
ITEM_COLS = ["item_id", "entity", "party_id", "party_name", "doc", "issue", "due", "ccy",
             "total", "open", "terms", "method", "po", "related", "carried"]


@dataclass
class Masters:
    customers: dict[str, dict]
    suppliers: dict[str, dict]
    entity_country: dict[str, str]
    terms: dict[str, tuple[int, int, int]]
    accounts: dict[str, dict]
    fx: FX


@dataclass
class Phase:
    name: str
    root: Path
    lines: list[Line]
    items: list[Item]
    masters: Masters
    golden: "pl.DataFrame | None"


def _csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _pct_bp(s: str) -> int:
    """'2.00' -> 200 basis points, no float."""
    s = (s or "").strip()
    if not s:
        return 0
    return cents(s)  # percent with 2 decimals == basis points


def load_masters(mdir: Path) -> Masters:
    customers = {}
    for r in _csv(mdir / "customers.csv"):
        customers[r["customer_id"]] = {
            "name": r.get("name", ""), "trade_name": r.get("trade_name", ""),
            "country": r.get("country", ""), "iban": (r.get("iban") or "").replace(" ", "").upper(),
            "parent": r.get("parent_customer_id", ""), "group": r.get("group_id", ""),
            "mandate": r.get("sepa_mandate_id", ""), "entity": r.get("entity", ""),
            "terms": r.get("payment_terms", ""), "ccy": r.get("invoice_currency", ""),
        }
    suppliers = {}
    for r in _csv(mdir / "suppliers.csv"):
        suppliers[r["supplier_id"]] = {
            "name": r.get("name", ""), "country": r.get("country", ""),
            "iban": (r.get("iban") or "").replace(" ", "").upper(), "entity": r.get("entity", ""),
            "account_number": r.get("account_number", ""), "method": r.get("payment_method", ""),
        }
    entity_country = {r["entity"]: r["country"] for r in _csv(mdir / "entities.csv")}
    terms = {r["code"]: (int(r["days"] or 0), _pct_bp(r.get("early_payment_discount_pct", "")),
                         int(r.get("discount_days") or 0)) for r in _csv(mdir / "payment_terms.csv")}
    accounts = {r["account_id"]: {"entity": r["entity"], "iban": (r.get("iban") or "").replace(" ", "").upper(),
                                  "ccy": r["currency"], "purpose": r.get("purpose", ""), "bank": r.get("bank", ""),
                                  "account_number": r.get("account_number", "")}
                for r in _csv(mdir / "bank_accounts.csv")}
    return Masters(customers, suppliers, entity_country, terms, accounts, FX(mdir))


def _read(path: Path, columns: list[str] | None = None) -> pl.DataFrame:
    return pl.read_csv(path, infer_schema_length=0, columns=columns)


def _norm_ar(df: pl.DataFrame) -> pl.DataFrame:
    return df.select(
        "item_id", "entity", pl.col("customer_id").alias("party_id"), pl.col("customer_name").alias("party_name"),
        pl.col("document_number").alias("doc"), pl.col("issue_date").alias("issue"), pl.col("due_date").alias("due"),
        pl.col("currency").alias("ccy"), pl.col("total_amount").alias("total"), pl.col("total_amount").alias("open"),
        pl.col("payment_terms").alias("terms"), pl.col("payment_method").alias("method"),
        pl.col("customer_po").alias("po"), pl.lit("").alias("related"), pl.lit("0").alias("carried"))


def _norm_ap(df: pl.DataFrame) -> pl.DataFrame:
    return df.select(
        "item_id", "entity", pl.col("supplier_id").alias("party_id"), pl.col("supplier_name").alias("party_name"),
        pl.col("supplier_invoice_number").alias("doc"), pl.col("issue_date").alias("issue"),
        pl.col("due_date").alias("due"), pl.col("currency").alias("ccy"), pl.col("total_amount").alias("total"),
        pl.col("total_amount").alias("open"), pl.col("payment_terms").alias("terms"),
        pl.col("payment_method").alias("method"), pl.col("our_po").alias("po"), pl.lit("").alias("related"),
        pl.lit("0").alias("carried"))


def _norm_cn(df: pl.DataFrame) -> pl.DataFrame:
    return df.select(
        "item_id", "entity", "party_id", "party_name", pl.col("document_number").alias("doc"),
        pl.col("issue_date").alias("issue"), pl.col("issue_date").alias("due"), pl.col("currency").alias("ccy"),
        pl.col("total_amount").alias("total"), pl.col("total_amount").alias("open"), pl.lit("").alias("terms"),
        pl.lit("").alias("method"), pl.lit("").alias("po"), pl.col("related_document").alias("related"),
        pl.lit("0").alias("carried"))


def _carried(root: Path) -> pl.DataFrame | None:
    """open_items_at_start rejoined to the TRAIN tables for terms/method/po/name/related."""
    path = root / "open_items_at_start.csv.gz"
    if not path.exists():
        return None
    o = _read(path)
    # FINAL carries items issued during LIVE: rejoin against every earlier phase's tables.
    frames = []
    for src in ("train", "live"):
        tr = DATA / src
        if tr == root or not (tr / "ar_invoices.csv.gz").exists():
            continue
        ar = _read(tr / "ar_invoices.csv.gz", ["item_id", "customer_name", "payment_terms", "payment_method", "customer_po"])
        ap = _read(tr / "ap_invoices.csv.gz", ["item_id", "supplier_name", "payment_terms", "payment_method", "our_po"])
        cn = _read(tr / "credit_notes.csv.gz", ["item_id", "party_name", "related_document"])
        frames += [
            ar.select("item_id", pl.col("customer_name").alias("party_name"), pl.col("payment_terms").alias("terms"),
                      pl.col("payment_method").alias("method"), pl.col("customer_po").alias("po"),
                      pl.lit("").alias("related")),
            ap.select("item_id", pl.col("supplier_name").alias("party_name"), pl.col("payment_terms").alias("terms"),
                      pl.col("payment_method").alias("method"), pl.col("our_po").alias("po"), pl.lit("").alias("related")),
            cn.select("item_id", "party_name", pl.lit("").alias("terms"), pl.lit("").alias("method"),
                      pl.lit("").alias("po"), pl.col("related_document").alias("related")),
        ]
    if not frames:
        print(f"[recon2] WARNING: no earlier-phase tables to rejoin carry-over in {root}", flush=True)
        o = o.with_columns(*[pl.lit("").alias(c) for c in ("terms", "method", "po", "related")])
    else:
        extra = pl.concat(frames).unique("item_id", keep="first", maintain_order=True)
        o = o.join(extra, on="item_id", how="left")
    return o.select(
        "item_id", "entity", "party_id", "party_name", pl.col("document_number").alias("doc"),
        pl.col("issue_date").alias("issue"), pl.col("due_date").alias("due"), pl.col("currency").alias("ccy"),
        pl.col("total_amount").alias("total"), pl.col("open_amount").alias("open"), "terms", "method", "po",
        "related", pl.lit("1").alias("carried"))


def _items(root: Path) -> list[Item]:
    frames = []
    if (root / "ar_invoices.csv.gz").exists():
        frames.append(_norm_ar(_read(root / "ar_invoices.csv.gz")))
    if (root / "ap_invoices.csv.gz").exists():
        frames.append(_norm_ap(_read(root / "ap_invoices.csv.gz")))
    if (root / "credit_notes.csv.gz").exists():
        frames.append(_norm_cn(_read(root / "credit_notes.csv.gz")))
    c = _carried(root)
    if c is not None:
        frames.append(c)
    df = pl.concat([f.select(ITEM_COLS) for f in frames]).fill_null("")
    df = df.unique("item_id", keep="first", maintain_order=True).sort("item_id")
    dcache: dict[str, int] = {}

    def dd(s: str) -> int:
        v = dcache.get(s)
        if v is None:
            v = dcache[s] = day(s)
        return v

    out: list[Item] = []
    cols = [df[c].to_list() for c in ITEM_COLS]
    for i, (iid, ent, pid, pname, doc, issue, due, ccy, tot, opn, terms, meth, po, rel, car) in enumerate(zip(*cols)):
        t, o = abs(cents(tot)), abs(cents(opn))
        carried = car == "1"
        out.append(Item(i, iid, iid[:3], ent, pid, pname, doc, dd(issue), dd(due), ccy, t, o, terms, meth, po, rel,
                        carried, carried and 0 < o < t))
    return out


def _lines(root: Path, lo: str | None = None, hi: str | None = None) -> list[Line]:
    df = _read(root / "bank_transactions.csv.gz").fill_null("")
    if lo:
        df = df.filter((pl.col("booking_date") >= lo) & (pl.col("booking_date") <= hi))
    df = df.sort(["booking_date", "bank_txn_id"])
    names = ["bank_txn_id", "account_id", "entity", "bank", "statement_format", "booking_date", "value_date",
             "amount", "currency", "bank_code", "bank_subcode", "ref1", "ref2", "text", "counterparty_name",
             "counterparty_account", "end_to_end_id", "remittance_info", "orig_amount", "orig_currency"]
    cols = [df[c].to_list() if c in df.columns else [""] * df.height for c in names]
    dcache: dict[str, int] = {}
    out: list[Line] = []
    for i, r in enumerate(zip(*cols)):
        (txn, acct, ent, bank, fmt, bd, vd, amt, ccy, code, sub, r1, r2, text, cn, ca, e2e, ri, oa, oc) = r
        bdo = dcache.get(bd) or dcache.setdefault(bd, day(bd))
        vdo = dcache.get(vd) or dcache.setdefault(vd, day(vd))
        ln = Line(i, txn, acct, ent, bank, fmt, bdo, vdo, cents(amt), ccy, abs(cents(oa)) if oa else None, oc,
                  code, sub, r1, r2, text, cn, ca, e2e, ri)
        build(ln)
        out.append(ln)
    return out


CLOSED_ON = Path(os.environ.get("RECON2_CLOSED_ON",
                                "/home/tete/.claude/jobs/253f644d/tmp/verifier/full_item_closed_on.parquet"))


def train_window(name: str) -> tuple[str, str]:
    """'train:2025-05' -> ('2025-05-01', '2025-05-31'); 'train:2025-03:2025-05' -> range."""
    parts = name.split(":")[1:]
    lo, hi = parts[0], parts[-1]
    y, m = int(hi[:4]), int(hi[5:7])
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()
    return lo + "-01", last


def _load_train_window(name: str) -> Phase:
    """Dev/eval phase cut out of TRAIN (golden available).  Items = issued <= window end and
    not closed more than 60 days before the window starts — the shape of LIVE's carry-over
    book.  Balances are replayed from golden by the engine (same as the sample)."""
    lo, hi = train_window(name)
    root = DATA / "train"
    masters = load_masters(DATA / "masters")
    lines = [ln for ln in _lines(root, lo, hi)]
    for i, ln in enumerate(lines):
        ln.idx = i
    keep = None
    if CLOSED_ON.exists():
        floor = (date.fromisoformat(lo) - timedelta(days=60))
        c = pl.read_parquet(CLOSED_ON).filter(pl.col("closed_on") < floor)
        keep = set(c["item_id"].to_list())  # items to DROP
    items = [it for it in _items(root) if it.issue <= day(hi) and (keep is None or it.item_id not in keep)]
    for i, it in enumerate(items):
        it.idx = i
    g = _read(root / "golden_allocations.csv.gz")
    txns = [ln.txn for ln in lines]
    golden = g.filter(pl.col("bank_txn_id").is_in(txns))
    return Phase(name, root, lines, items, masters, golden)


def _load_final_sim() -> Phase:
    """FINAL dry run: LIVE's last booking month as the lines, data/masters as masters, LIVE's own
    item tables.  Exercises strict mode / the loader / ops / advices on the final code path
    without FINAL data.  Not scored (golden is None)."""
    root = DATA / "live"
    df = _read(root / "bank_transactions.csv.gz", ["booking_date"])
    last = max(df["booking_date"].to_list())[:7]
    lines = [ln for ln in _lines(root, last + "-01", last + "-31")]
    for i, ln in enumerate(lines):
        ln.idx = i
    return Phase("final_sim", root, lines, _items(root), load_masters(DATA / "masters"), None)


def load_phase(name: str) -> Phase:
    if name.startswith("train:"):
        return _load_train_window(name)
    if name not in PHASES:
        raise ValueError(f"unknown phase {name!r}")
    if name == "final_sim":
        return _load_final_sim()
    root = DATA / name
    if not root.exists():
        raise FileNotFoundError(root)
    mdir = root / "masters_final" if (root / "masters_final").exists() else DATA / "masters"
    masters = load_masters(mdir)
    lines = _lines(root)
    items = _items(root)
    g = root / "golden_allocations.csv.gz"
    golden = _read(g) if g.exists() else None
    return Phase(name, root, lines, items, masters, golden)
