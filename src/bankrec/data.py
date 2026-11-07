"""Reading the data dumps with **polars** (see DATA_DICTIONARY.md).

Two rules for the whole project:

1. **Read everything as text, convert money once.** The dumps store amounts as strings;
   `to_cents()` is the only place that turns "1210.00" into an integer. Integer cents
   avoid the float rounding that would make you miss the scorer's 0.5 % tolerance.
2. **Polars for bulk work, Python only for the candidates.** Reading, filtering, joins
   and the master lookups are vectorised; the per-bank-line matching loop only touches
   the handful of items a line can plausibly settle.

Layout produced by scripts/download_data.sh under data/raw/:

    masters/                 customers, suppliers, bank_accounts, payment_terms, fx_rates…
    sample/  train/  live/  final/
        bank_transactions.csv.gz      one row per bank line (the thing you must explain)
        ar_invoices.csv.gz            customer invoices  (ARI-…)
        ap_invoices.csv.gz            supplier invoices  (API-…)
        credit_notes.csv.gz           credit notes       (ARC-… / APC-…)
        open_items_at_start.csv.gz    LIVE/FINAL only: items open when the phase starts
        golden_allocations.csv.gz     TRAIN/sample only: the labels
        bank_statements/ direct_debit_batches/ card_settlements/ cheque_deposits/
        payment_runs/ remittance_advices/ pdfs/     the supporting documents
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .config import Settings

# Every column as string: no dtype guessing, no locale surprises.
READ = dict(infer_schema_length=0, null_values=[""], truncate_ragged_lines=True)


def to_cents(column: str = "value") -> pl.Expr:
    """Polars expression: '1210.00' -> 121000 (Int64). Empty -> 0."""
    return (
        pl.col(column)
        .cast(pl.String)
        .str.strip_chars()
        .replace("", "0")
        .cast(pl.Float64)
        .mul(100)
        .round()
        .cast(pl.Int64)
    )


def read_table(path: Path, columns: list[str] | None = None) -> pl.DataFrame:
    """Read a (possibly gzipped) dump as text. Raises if the file is missing."""
    if not path.exists():
        raise FileNotFoundError(f"{path} — run scripts/download_data.sh first")
    return pl.read_csv(path, columns=columns, **READ)


def read_optional(path: Path, columns: list[str] | None = None) -> pl.DataFrame:
    return read_table(path, columns) if path.exists() else pl.DataFrame()


def _alnum(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


# --------------------------------------------------------------- masters


class Masters:
    """Master data plus the dictionaries you will query on every bank line.

    Building these once matters: a naive `@property` that re-scans 60k customers per
    line turns a 30-second run into a 30-minute one.
    """

    def __init__(self, root: Path):
        self.customers = read_optional(root / "customers.csv")
        self.suppliers = read_optional(root / "suppliers.csv")
        self.bank_accounts = read_optional(root / "bank_accounts.csv")
        self.payment_terms = read_optional(root / "payment_terms.csv")
        self.fx_rates = read_optional(root / "fx_rates.csv")
        categories = read_optional(root / "categories.csv")
        self.categories: list[str] = categories["category"].to_list() if not categories.is_empty() else []

        # payment term code -> {days, early_payment_discount_pct, discount_days}
        self.terms: dict[str, dict] = {
            row["code"]: row for row in self.payment_terms.iter_rows(named=True)
        }

        # known paying IBAN -> (party_id, side). POLICIES.md §5.1.
        self.iban_owner: dict[str, tuple[str, str]] = {}
        for side, df, id_col in (("AR", self.customers, "customer_id"), ("AP", self.suppliers, "supplier_id")):
            if df.is_empty() or "iban" not in df.columns:
                continue
            for row in df.iter_rows(named=True):
                if row.get("iban"):
                    self.iban_owner[_alnum(row["iban"])] = (row[id_col], side)

        # our own IBAN -> account row (internal transfer / cash pool / intercompany)
        self.own_accounts: dict[str, dict] = {}
        if not self.bank_accounts.is_empty():
            for row in self.bank_accounts.iter_rows(named=True):
                if row.get("iban"):
                    self.own_accounts[_alnum(row["iban"])] = row

        # party id -> current name (bank texts use truncated/trade names)
        self.party_names: dict[str, str] = {}
        for df, id_col in ((self.customers, "customer_id"), (self.suppliers, "supplier_id")):
            if df.is_empty():
                continue
            for row in df.iter_rows(named=True):
                self.party_names[row[id_col]] = row.get("name") or ""


# ------------------------------------------------------------------- fx


class FX:
    """ECB reference rates (units of currency per EUR), with 'last available day'."""

    def __init__(self, rates: pl.DataFrame):
        self._rates: dict[str, tuple[list[int], list[float]]] = {}
        if rates.is_empty():
            return
        df = rates.with_columns(pl.col("units_per_eur").cast(pl.Float64))
        for currency, group in df.sort("date").group_by("currency", maintain_order=True):
            dates = [int(d.replace("-", "")) for d in group["date"].to_list()]
            rates_ = group["units_per_eur"].to_list()
            self._rates[str(currency)] = (dates, rates_)

    def rate(self, currency: str, ymd: int) -> float | None:
        if currency == "EUR":
            return 1.0
        series = self._rates.get(currency)
        if not series:
            return None
        days, rates = series
        return rates[max(bisect.bisect_right(days, ymd) - 1, 0)]

    def convert(self, cents: int, src: str, dst: str, ymd: int) -> int | None:
        """Convert between currencies via EUR. None when a rate is unknown."""
        if src == dst:
            return cents
        src_rate, dst_rate = self.rate(src, ymd), self.rate(dst, ymd)
        if not src_rate or not dst_rate:
            return None
        return int(round(cents / src_rate * dst_rate))


# ------------------------------------------------------------------ phase


@dataclass(slots=True)
class Phase:
    """Everything you need for one phase (sample | train | live | final)."""

    settings: Settings
    name: str | None = None

    def __post_init__(self) -> None:
        self.name = self.name or self.settings.phase

    @property
    def dir(self) -> Path:
        return self.settings.data_dir / self.name

    # -------------------------------------------------------------- tables
    def bank_transactions(self) -> pl.DataFrame:
        """One row per bank line, sorted by booking date, with `amount_cents` (signed)."""
        df = read_table(self.dir / "bank_transactions.csv.gz")
        df = df.with_columns(to_cents("amount").alias("amount_cents"))
        for col in ("ref1", "ref2", "text", "remittance_info", "counterparty_name", "counterparty_account", "end_to_end_id"):
            if col not in df.columns:
                df = df.with_columns(pl.lit("").alias(col))
        return df.sort(["booking_date", "bank_txn_id"])

    def items(self, phases: list[str] | None = None) -> pl.DataFrame:
        """Canonical open-item table for the given phases (default: the phase itself).

        Columns: item_id, side (AR/AP), kind, entity, party_id, party_name,
        document_number, issue_date, due_date, currency, total_cents, open_cents,
        payment_terms, related_document.
        """
        phases = phases or self.settings.item_phases
        frames = [self._items_for(p) for p in phases]
        frames = [f for f in frames if not f.is_empty()]
        if not frames:
            return pl.DataFrame()
        return pl.concat(frames, how="diagonal").unique(subset=["item_id"], keep="last", maintain_order=True)

    def _items_for(self, phase: str) -> pl.DataFrame:
        d = self.settings.data_dir / phase
        frames: list[pl.DataFrame] = []

        ar = read_optional(d / "ar_invoices.csv.gz")
        if not ar.is_empty():
            frames.append(
                ar.select(
                    pl.col("item_id"),
                    pl.lit("AR").alias("side"),
                    pl.lit("invoice").alias("kind"),
                    pl.col("entity"),
                    pl.col("customer_id").alias("party_id"),
                    pl.col("customer_name").alias("party_name"),
                    pl.col("document_number"),
                    pl.col("issue_date"),
                    pl.col("due_date"),
                    pl.col("currency"),
                    to_cents("total_amount").alias("total_cents"),
                    to_cents("total_amount").alias("open_cents"),
                    pl.col("payment_terms"),
                    pl.lit("").alias("related_document"),
                )
            )

        ap = read_optional(d / "ap_invoices.csv.gz")
        if not ap.is_empty():
            frames.append(
                ap.select(
                    pl.col("item_id"),
                    pl.lit("AP").alias("side"),
                    pl.lit("invoice").alias("kind"),
                    pl.col("entity"),
                    pl.col("supplier_id").alias("party_id"),
                    pl.col("supplier_name").alias("party_name"),
                    pl.col("supplier_invoice_number").alias("document_number"),
                    pl.col("issue_date"),
                    pl.col("due_date"),
                    pl.col("currency"),
                    to_cents("total_amount").alias("total_cents"),
                    to_cents("total_amount").alias("open_cents"),
                    pl.col("payment_terms"),
                    pl.lit("").alias("related_document"),
                )
            )

        cn = read_optional(d / "credit_notes.csv.gz")
        if not cn.is_empty():
            frames.append(
                cn.select(
                    pl.col("item_id"),
                    pl.col("side"),
                    pl.lit("credit_note").alias("kind"),
                    pl.col("entity"),
                    pl.col("party_id"),
                    pl.col("party_name"),
                    pl.col("document_number"),
                    pl.col("issue_date"),
                    pl.col("issue_date").alias("due_date"),
                    pl.col("currency"),
                    to_cents("total_amount").alias("total_cents"),
                    to_cents("total_amount").alias("open_cents"),
                    pl.lit("").alias("payment_terms"),
                    pl.col("related_document"),
                )
            )

        # LIVE/FINAL start with the items that were still open when the phase began.
        oi = read_optional(d / "open_items_at_start.csv.gz")
        if not oi.is_empty():
            frames.append(
                oi.select(
                    pl.col("item_id"),
                    pl.col("item_id").str.slice(0, 3).replace_strict(
                        {"ARI": "AR", "ARC": "AR", "API": "AP", "APC": "AP"}, default=""
                    ).alias("side"),
                    pl.col("item_type").alias("kind"),
                    pl.col("entity"),
                    pl.col("party_id"),
                    pl.lit("").alias("party_name"),
                    pl.col("document_number"),
                    pl.col("issue_date"),
                    pl.col("due_date"),
                    pl.col("currency"),
                    to_cents("total_amount").alias("total_cents"),
                    to_cents("open_amount").alias("open_cents"),
                    pl.lit("").alias("payment_terms"),
                    pl.lit("").alias("related_document"),
                )
            )

        return pl.concat(frames, how="diagonal") if frames else pl.DataFrame()

    # --------------------------------------------------------------- masters
    @property
    def masters(self) -> Masters:
        return Masters(self.settings.data_dir / "masters")

    # ------------------------------------------------------------ documents
    def documents(self, folder: str) -> pl.DataFrame:
        """Concatenate a document folder: direct_debit_batches, card_settlements,
        cheque_deposits, payment_runs (one CSV per entity and month)."""
        d = self.dir / folder
        if not d.is_dir():
            return pl.DataFrame()
        files = sorted(d.glob("*.csv"))
        if not files:
            return pl.DataFrame()
        return pl.concat([read_table(f) for f in files], how="diagonal")

    def golden(self) -> pl.DataFrame:
        """TRAIN/sample labels: bank_txn_id, item_id, amount, category, exception, segment, difficulty."""
        return read_table(self.dir / "golden_allocations.csv.gz").with_columns(
            to_cents("amount").alias("amount_cents")
        )

    def has_golden(self) -> bool:
        return (self.dir / "golden_allocations.csv.gz").exists()


__all__ = ["Phase", "Masters", "FX", "read_table", "read_optional", "to_cents", "pl"]
