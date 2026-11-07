"""The submission contract, copied verbatim from FORMAT.md (do not invent values)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- submission

SUBMISSION_COLUMNS = ["bank_txn_id", "item_id", "amount", "category", "exception"]

MAX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_ROWS = 3_000_000

ITEM_ID_RE = re.compile(r"^(ARI|ARC|API|APC)-\d{4}-\d{7}$")
BANK_TXN_RE = re.compile(r"^BT-\d{4}-\d{7}$")

# Closed list (POLICIES.md §3).
EXCEPTIONS = [
    "PARTIAL",
    "OVERPAYMENT",
    "SHORT_DISCOUNT",
    "SHORT_BANK_CHARGES",
    "SHORT_WITHHOLDING",
    "SHORT_DISPUTE",
    "DUPLICATE_PAYMENT",
    "UNIDENTIFIED",
    "RETURNED",
    "FX_DIFFERENCE",
]

# Exceptions that mean "the item IS fully cleared, the cash just differs".
FULL_CLEARING_EXCEPTIONS = {
    "SHORT_DISCOUNT",
    "SHORT_BANK_CHARGES",
    "SHORT_WITHHOLDING",
    "OVERPAYMENT",
    "FX_DIFFERENCE",
}

# Exceptions whose amount is what was actually applied, not the open amount.
APPLIED_AMOUNT_EXCEPTIONS = {"PARTIAL", "SHORT_DISPUTE", "DUPLICATE_PAYMENT", "RETURNED"}

# Closed list (masters/categories.csv).
CATEGORIES = [
    "BANK_FEES",
    "INTEREST_INCOME",
    "INTEREST_EXPENSE",
    "PAYROLL",
    "SOCIAL_SECURITY",
    "TAX_PAYMENT",
    "TAX_REFUND",
    "LOAN_DRAWDOWN",
    "LOAN_REPAYMENT",
    "LEASING",
    "FX_DEAL",
    "CASH_POOL",
    "INTERCOMPANY",
    "INTERNAL_TRANSFER",
    "CUSTOMER_ADVANCE",
    "CUSTOMER_REFUND",
    "SUPPLIER_REFUND",
    "CARD_EXPENSES",
    "INSURANCE",
    "BANK_REVERSAL",
    "UNIDENTIFIED",
]

# Withholding tax rates that appear in POLICIES.md §3 (percentage of invoice total).
WITHHOLDING_RATES = (0.10, 0.015, 0.0125)
# Intermediary-bank charge tolerance (SHA/BEN): <= 60 EUR and <= 2% of the item.
BANK_CHARGE_MAX_EUR = 60.0
BANK_CHARGE_MAX_PCT = 0.02
# Early-payment discount grace period (days) after the discount deadline.
DISCOUNT_GRACE_DAYS = 3

# Item families.
AR_INVOICE_PREFIX = "ARI"
AR_CREDIT_PREFIX = "ARC"
AP_INVOICE_PREFIX = "API"
AP_CREDIT_PREFIX = "APC"
CREDIT_PREFIXES = {AR_CREDIT_PREFIX, AP_CREDIT_PREFIX}


@dataclass(frozen=True, slots=True)
class SubmissionRow:
    """One allocation row. `amount` is a positive decimal string in item currency."""

    bank_txn_id: str
    item_id: str = ""
    amount: str = ""
    category: str = ""
    exception: str = ""

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (self.bank_txn_id, self.item_id, self.amount, self.category, self.exception)


def validate_row(row: SubmissionRow, *, phase_txns: set[str] | None = None) -> list[str]:
    """Cheap pre-flight check; the server scorer in vendor/ is the source of truth."""
    errs: list[str] = []
    if not BANK_TXN_RE.match(row.bank_txn_id):
        errs.append(f"bad bank_txn_id {row.bank_txn_id!r}")
    if phase_txns is not None and row.bank_txn_id not in phase_txns:
        errs.append(f"{row.bank_txn_id} not in phase")
    if row.item_id:
        if not ITEM_ID_RE.match(row.item_id):
            errs.append(f"bad item_id {row.item_id!r}")
        if not row.amount:
            errs.append("amount required when item_id is set")
        if row.category:
            errs.append("category must be empty on item rows")
    else:
        if row.amount:
            errs.append("amount must be empty on no-item rows")
        if row.category and row.category not in CATEGORIES:
            errs.append(f"unknown category {row.category!r}")
    if row.exception and row.exception not in EXCEPTIONS:
        errs.append(f"unknown exception {row.exception!r}")
    return errs
