"""Loading the hackathon data dumps into canonical tables.

Everything is read as text first (the dumps are UTF-8 CSV, optionally .csv.gz) and
amounts are converted to integer cents exactly once, at the boundary.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pandas as pd

from .config import Settings
from .schema import CATEGORIES, EXCEPTIONS

STR_READ = dict(dtype=str, keep_default_na=False, na_filter=False)


# ---------------------------------------------------------------- primitives


def to_cents(series: pd.Series) -> pd.Series:
    """'1210.00' -> 121000. NaN/'' -> 0. Sign preserved."""
    cleaned = series.fillna("").astype(str).str.strip().replace({"": "0"})
    return (cleaned.astype(float) * 100).round().astype("int64")


def read_table(path: Path, usecols: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    return pd.read_csv(path, usecols=usecols, **STR_READ)


def read_optional(path: Path, usecols: list[str] | None = None) -> pd.DataFrame:
    return read_table(path, usecols) if path.exists() else pd.DataFrame()


@dataclass
class Masters:
    entities: pd.DataFrame
    bank_accounts: pd.DataFrame
    customers: pd.DataFrame
    suppliers: pd.DataFrame
    payment_terms: pd.DataFrame
    fx_rates: pd.DataFrame
    categories: list[str]

    # -------------------------------------------------------------- helpers
    @cached_property
    def accounts_by_id(self) -> dict[str, dict]:
        return {r.account_id: r._asdict() for r in self.bank_accounts.itertuples(index=False)}

    @cached_property
    def customer_by_id(self) -> dict[str, dict]:
        return {r.customer_id: r._asdict() for r in self.customers.itertuples(index=False)}

    @cached_property
    def supplier_by_id(self) -> dict[str, dict]:
        return {r.supplier_id: r._asdict() for r in self.suppliers.itertuples(index=False)}

    @cached_property
    def iban_owner(self) -> dict[str, tuple[str, str]]:
        """Normalised IBAN -> (party_id, side AR/AP)."""
        out: dict[str, tuple[str, str]] = {}
        for r in self.customers.itertuples(index=False):
            if r.iban:
                out[_iban(r.iban)] = (r.customer_id, "AR")
        for r in self.suppliers.itertuples(index=False):
            if r.iban:
                out[_iban(r.iban)] = (r.supplier_id, "AP")
            if r.account_number:
                out[_iban(r.account_number)] = (r.supplier_id, "AP")
        return out

    @cached_property
    def group_members(self) -> dict[str, set[str]]:
        """group_id -> every customer_id in that group (parents may pay for subsidiaries)."""
        out: dict[str, set[str]] = {}
        for r in self.customers.itertuples(index=False):
            gid = r.group_id or r.parent_customer_id
            if gid:
                out.setdefault(gid, set()).add(r.customer_id)
        return out

    @cached_property
    def customer_groups(self) -> dict[str, str]:
        return {
            r.customer_id: (r.group_id or r.parent_customer_id)
            for r in self.customers.itertuples(index=False)
            if (r.group_id or r.parent_customer_id)
        }

    @cached_property
    def terms(self) -> dict[str, dict]:
        return {r.code: r._asdict() for r in self.payment_terms.itertuples(index=False)}


def _iban(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


# ------------------------------------------------------------------ phase


@dataclass
class PhaseData:
    root: Path
    phase: str

    @property
    def dir(self) -> Path:
        return self.root / self.phase

    # -------------------------------------------------------------- tables
    def bank_transactions(self) -> pd.DataFrame:
        df = read_table(self.dir / "bank_transactions.csv.gz")
        df["amount_cents"] = to_cents(df["amount"])
        # One normalised text blob used by blocking (all reference-ish fields).
        for col in ("ref1", "ref2", "text", "remittance_info", "counterparty_name", "end_to_end_id", "counterparty_account"):
            if col not in df.columns:
                df[col] = ""
            df[col] = df[col].fillna("")
        df["booking_date"] = df["booking_date"].fillna("")
        df = df.sort_values(["booking_date", "bank_txn_id"], kind="stable").reset_index(drop=True)
        return df

    def items(self, phases: list[str]) -> pd.DataFrame:
        frames = [self._items_for(p) for p in phases]
        items = pd.concat([f for f in frames if not f.empty], ignore_index=True)
        # Items present in open_items_at_start are authoritative for balances.
        if "open_amount_cents" in items.columns:
            items["open_amount_cents"] = items["open_amount_cents"].fillna(items["total_amount_cents"])
        return items

    def _items_for(self, phase: str) -> pd.DataFrame:
        d = self.root / phase
        frames: list[pd.DataFrame] = []

        ar = read_optional(d / "ar_invoices.csv.gz")
        if not ar.empty:
            frames.append(
                pd.DataFrame(
                    {
                        "item_id": ar["item_id"],
                        "side": "AR",
                        "kind": "invoice",
                        "entity": ar["entity"],
                        "party_id": ar["customer_id"],
                        "party_name": ar["customer_name"],
                        "document_number": ar["document_number"],
                        "issue_date": ar["issue_date"],
                        "due_date": ar["due_date"],
                        "currency": ar["currency"],
                        "total_amount_cents": to_cents(ar["total_amount"]),
                        "open_amount_cents": to_cents(ar["total_amount"]),
                        "payment_terms": ar.get("payment_terms", ""),
                        "related_document": "",
                        "payment_method": ar.get("payment_method", ""),
                        "customer_po": ar.get("customer_po", ""),
                        "source_phase": phase,
                    }
                )
            )

        ap = read_optional(d / "ap_invoices.csv.gz")
        if not ap.empty:
            frames.append(
                pd.DataFrame(
                    {
                        "item_id": ap["item_id"],
                        "side": "AP",
                        "kind": "invoice",
                        "entity": ap["entity"],
                        "party_id": ap["supplier_id"],
                        "party_name": ap["supplier_name"],
                        "document_number": ap["supplier_invoice_number"],
                        "issue_date": ap["issue_date"],
                        "due_date": ap["due_date"],
                        "currency": ap["currency"],
                        "total_amount_cents": to_cents(ap["total_amount"]),
                        "open_amount_cents": to_cents(ap["total_amount"]),
                        "payment_terms": ap.get("payment_terms", ""),
                        "related_document": "",
                        "payment_method": ap.get("payment_method", ""),
                        "customer_po": ap.get("our_po", ""),
                        "source_phase": phase,
                    }
                )
            )

        cn = read_optional(d / "credit_notes.csv.gz")
        if not cn.empty:
            frames.append(
                pd.DataFrame(
                    {
                        "item_id": cn["item_id"],
                        "side": cn["side"],
                        "kind": "credit_note",
                        "entity": cn["entity"],
                        "party_id": cn["party_id"],
                        "party_name": cn["party_name"],
                        "document_number": cn["document_number"],
                        "issue_date": cn["issue_date"],
                        "due_date": cn["issue_date"],
                        "currency": cn["currency"],
                        "total_amount_cents": to_cents(cn["total_amount"]),
                        "open_amount_cents": to_cents(cn["total_amount"]),
                        "payment_terms": "",
                        "related_document": cn.get("related_document", ""),
                        "payment_method": "",
                        "customer_po": "",
                        "source_phase": phase,
                    }
                )
            )

        oi = read_optional(d / "open_items_at_start.csv.gz")
        if not oi.empty:
            frames.append(
                pd.DataFrame(
                    {
                        "item_id": oi["item_id"],
                        "side": oi["item_id"].str[:3].map({"ARI": "AR", "ARC": "AR", "API": "AP", "APC": "AP"}),
                        "kind": oi["item_type"],
                        "entity": oi["entity"],
                        "party_id": oi["party_id"],
                        "party_name": "",
                        "document_number": oi["document_number"],
                        "issue_date": oi["issue_date"],
                        "due_date": oi["due_date"],
                        "currency": oi["currency"],
                        "total_amount_cents": to_cents(oi["total_amount"]),
                        "open_amount_cents": to_cents(oi["open_amount"]),
                        "payment_terms": "",
                        "related_document": "",
                        "payment_method": "",
                        "customer_po": "",
                        "source_phase": phase,
                    }
                )
            )

        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        # open_items_at_start wins over the reconstructed total for the same item.
        df = df.drop_duplicates(subset=["item_id"], keep="last")
        df["open_amount_cents"] = df["open_amount_cents"].fillna(df["total_amount_cents"])
        for col in ("payment_terms", "related_document", "payment_method", "customer_po"):
            df[col] = df[col].fillna("")
        return df

    # ------------------------------------------------------------- masters
    @cached_property
    def masters(self) -> Masters:
        m = self.root / "masters"
        categories = CATEGORIES
        cat_path = m / "categories.csv"
        if cat_path.exists():
            categories = read_table(cat_path)["category"].tolist()
        return Masters(
            entities=read_optional(m / "entities.csv"),
            bank_accounts=read_optional(m / "bank_accounts.csv"),
            customers=read_optional(m / "customers.csv"),
            suppliers=read_optional(m / "suppliers.csv"),
            payment_terms=read_optional(m / "payment_terms.csv"),
            fx_rates=read_optional(m / "fx_rates.csv"),
            categories=categories,
        )

    # --------------------------------------------------- auxiliary documents
    def documents(self, kind: str) -> pd.DataFrame:
        """kind in {direct_debit_batches, card_settlements, cheque_deposits, payment_runs}."""
        d = self.dir / kind
        if not d.is_dir():
            return pd.DataFrame()
        files = sorted(d.glob("*.csv"))
        if not files:
            return pd.DataFrame()
        frames = [read_table(f) for f in files]
        return pd.concat(frames, ignore_index=True)

    def document_index(self) -> pd.DataFrame:
        return read_optional(self.dir / "pdfs" / "index.csv")

    def remittance_index(self) -> pd.DataFrame:
        return read_optional(self.dir / "remittance_advices" / "index.csv")

    # --------------------------------------------------------------- labels
    def golden(self) -> pd.DataFrame:
        path = self.dir / "golden_allocations.csv.gz"
        df = read_table(path)
        df["amount_cents"] = to_cents(df["amount"])
        return df

    def has_golden(self) -> bool:
        return (self.dir / "golden_allocations.csv.gz").exists()


# ------------------------------------------------------------------ settings


def phase_data(settings: Settings, phase: str | None = None) -> PhaseData:
    return PhaseData(settings.data_dir, phase or settings.run.phase)


__all__ = [
    "Masters",
    "PhaseData",
    "phase_data",
    "read_table",
    "read_optional",
    "to_cents",
    "gzip",
    "EXCEPTIONS",
    "CATEGORIES",
]
