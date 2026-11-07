"""The contract with the scorer — copied from FORMAT.md / POLICIES.md.

Nothing in here is a heuristic: these are the closed lists and limits the server
enforces. Keep them in sync with the official docs (the scorer in ../vendor is the
source of truth).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- submission
# One row per allocation (a bank line can settle several items).
SUBMISSION_COLUMNS = ["bank_txn_id", "item_id", "amount", "category", "exception"]

# Submission limits (FORMAT.md).
MAX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_ROWS = 3_000_000

# Id shapes. Item ids are ARI/ARC/API/APC + issue year + 7 digits.
ITEM_ID_RE = re.compile(r"^(ARI|ARC|API|APC)-\d{4}-\d{7}$")
BANK_TXN_RE = re.compile(r"^BT-\d{4}-\d{7}$")

# Closed exception list (POLICIES.md §3). Empty string = no exception.
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

# Closed category list (masters/categories.csv, POLICIES.md §4).
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

# Score weights: 100 * (0.5 * allocation F1 + 0.3 * txn accuracy + 0.2 * macro-F1).
W_ALLOCATION = 0.50
W_TRANSACTION = 0.30
W_EXCEPTION = 0.20

# An amount is correct when |submitted - golden| <= max(0.01, 0.5% of golden).
AMOUNT_TOLERANCE_PCT = 0.005
AMOUNT_TOLERANCE_MIN_CENTS = 1

# Policy constants you will need when you implement the exceptions (POLICIES.md §3).
WITHHOLDING_RATES = (0.10, 0.015, 0.0125)  # 10 %, 1.5 %, 1.25 % of invoice total
BANK_CHARGE_MAX_EUR = 60.0                  # intermediary-bank fee tolerance
BANK_CHARGE_MAX_PCT = 0.02                  # ... also <= 2 % of the item
DISCOUNT_GRACE_DAYS = 3                     # after invoice date + discount days

# Item families.
CREDIT_NOTE_PREFIXES = {"ARC", "APC"}
SIDE_BY_PREFIX = {"ARI": "AR", "ARC": "AR", "API": "AP", "APC": "AP"}


def amount_tolerance_cents(golden_cents: int) -> int:
    """Absolute tolerance in cents, exactly like vendor/bankrec_score.py."""
    return max(AMOUNT_TOLERANCE_MIN_CENTS, int(round(AMOUNT_TOLERANCE_PCT * abs(golden_cents))))


def validate_row(row: tuple[str, str, str, str, str], phase_txns: set[str] | None = None) -> list[str]:
    """Pre-flight checks for one row. Returns a list of human-readable problems."""
    txn, item, amount, category, exception = row
    errs: list[str] = []
    if not BANK_TXN_RE.match(txn):
        errs.append(f"bad bank_txn_id {txn!r}")
    if phase_txns is not None and txn not in phase_txns:
        errs.append(f"{txn} is not a bank line of this phase")
    if item:
        if not ITEM_ID_RE.match(item):
            errs.append(f"bad item_id {item!r}")
        if not amount:
            errs.append("amount is required when item_id is set")
        elif not re.match(r"^\d+(\.\d{1,2})?$", amount):
            errs.append(f"amount must be a positive decimal with a dot ({amount!r})")
        if category:
            errs.append("category must be empty on item rows")
    else:
        if amount:
            errs.append("amount must be empty on no-item rows")
        if category and category not in CATEGORIES:
            errs.append(f"unknown category {category!r}")
    if exception and exception not in EXCEPTIONS:
        errs.append(f"unknown exception {exception!r}")
    return errs
