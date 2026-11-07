"""POLICIES.md as code: exceptions per row and categories for no-item lines.

The rules are intentionally explicit and ordered; when the evidence is ambiguous the
code returns an empty exception rather than a wrong one (the scorer punishes wrong
exceptions twice: transaction accuracy and macro-F1).
"""

from __future__ import annotations

import unicodedata
from datetime import date, timedelta

from .config import Settings
from .data import Masters
from .items import FXTable, ItemRef
from .schema import (
    BANK_CHARGE_MAX_EUR,
    BANK_CHARGE_MAX_PCT,
    DISCOUNT_GRACE_DAYS,
    WITHHOLDING_RATES,
)

# --------------------------------------------------------------------- text


def _fold(value: str) -> str:
    norm = unicodedata.normalize("NFKD", (value or "").upper())
    return "".join(ch for ch in norm if not unicodedata.combining(ch))


def has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _ymd_to_date(value: int) -> date | None:
    if not value:
        return None
    try:
        return date(value // 10000, (value // 100) % 100, value % 100)
    except ValueError:
        return None


# ---------------------------------------------------------------- exceptions

RETURN_MARKERS = (
    "DEVUELTO", "DEVOLUCION", "RETURN", "RETOUR", "RUECK", "RUCKBUCHUNG", "REJET",
    "IMPAGADO", "IMPAGO", "REJECTED", "CHARGEBACK", "STORNO", "ANULACION OPERACION",
)
DISPUTE_MARKERS = (
    "DISPUTA", "DISPUTE", "RECLAMACION", "RECLAMO", "LITIGE", "RETENCION JUDICIAL",
    "PENALIZACION", "DAMAGED", "SHORT DELIVERY", "PRICE DIFFERENCE", "COOP",
    "MARKETING", "LOGISTICS FEE", "ABZUG", "MAHNUNG",
)


def exception_for(
    *,
    txn: dict,
    item: ItemRef,
    applied_cents: int,
    remaining_cents: int,
    masters: Masters,
    settings: Settings,
    fx: FXTable | None = None,
) -> str:
    """Decide the exception code for one (bank line, item) row."""
    applied = max(int(applied_cents), 0)
    remaining = max(int(remaining_cents), 0)
    open_before = applied + remaining or applied
    tol = settings.scoring.tolerant(open_before)
    text = _fold(
        " ".join(
            str(txn.get(k) or "")
            for k in ("text", "ref1", "ref2", "remittance_info", "counterparty_name")
        )
    )
    booking = _ymd_to_date(int(str(txn.get("booking_date") or "").replace("-", "")[:8] or 0) or 0)
    txn_currency = str(txn.get("currency") or "EUR")

    # 1. The item is NOT cleared: PARTIAL, or SHORT_DISPUTE when the advice says why.
    if remaining > tol:
        if has_any(text, DISPUTE_MARKERS):
            return "SHORT_DISPUTE"
        return "PARTIAL"

    # 2. The item is cleared but the cash differs.
    diff = open_before - applied  # >0 shortfall, <0 overpayment
    if item.kind == "credit_note":
        # credit-note rows carry no exception (FX included)
        return ""

    if txn_currency != item.currency and applied == open_before:
        return "FX_DIFFERENCE"

    if diff < -tol:
        return "OVERPAYMENT"

    if diff <= tol:
        return ""

    # 2a. Early-payment discount, within terms + 3 days of grace.
    term = masters.terms.get(item.terms or "")
    if term and term.get("early_payment_discount_pct"):
        try:
            pct = float(term["early_payment_discount_pct"]) / 100.0
            discount_days = int(term.get("discount_days") or 0)
        except (TypeError, ValueError):
            pct, discount_days = 0.0, 0
        if pct and booking and item.issue_ymd:
            issued = _ymd_to_date(item.issue_ymd)
            deadline = issued + timedelta(days=discount_days + DISCOUNT_GRACE_DAYS) if issued else None
            expected = int(round(item.total_cents * (1 - pct)))
            if deadline and booking <= deadline and abs(applied - expected) <= tol:
                return "SHORT_DISCOUNT"

    # 2b. Withholding tax at source (1.25 / 1.5 / 10 % of the invoice total).
    for rate in WITHHOLDING_RATES:
        expected = int(round(item.total_cents * (1 - rate)))
        if abs(applied - expected) <= tol:
            return "SHORT_WITHHOLDING"

    # 2c. Intermediary bank charges: shortfall <= 60 EUR and <= 2% of the item.
    if diff <= max(tol, int(round(item.total_cents * BANK_CHARGE_MAX_PCT))):
        fx_table = fx or FXTable(masters.fx_rates)
        shortfall_eur = fx_table.to_eur(diff, item.currency, int(item.issue_ymd or 0))
        if shortfall_eur is not None and shortfall_eur <= BANK_CHARGE_MAX_EUR:
            return "SHORT_BANK_CHARGES"

    # 2d. Fully cleared inside the scorer tolerance (rounding, small FX move).
    return ""


def is_return(txn: dict) -> bool:
    text = _fold(
        " ".join(str(txn.get(k) or "") for k in ("text", "ref1", "ref2", "remittance_info"))
    )
    return has_any(text, RETURN_MARKERS)


# ---------------------------------------------------------------- categories

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("BANK_REVERSAL", ("ANULACION OPERACION INDEBIDA", "ANULACION", "STORNO", "REVERSAL", "RECHARGE", "ANNULATION", "CANCELACION OPERACION")),
    ("CASH_POOL", ("CASH POOL", "POOLING", "BARRIDO", "SWEEP", "ZERO BALANCE", "ZB ", "CASH MANAGEMENT")),
    ("FX_DEAL", ("DIVISA", "DEVISEN", "CHANGE", "CAMBIO", "FX ", "SPOT", "FOREX", "CONVERSION")),
    ("PAYROLL", ("NOMINA", "NOMINAS", "SALARIO", "SALARIOS", "GEHALT", "LOHN", "SALAIRE", "PAYE", "WAGES", "PAYROLL", "PENSION")),
    ("SOCIAL_SECURITY", ("TGSS", "SEGURIDAD SOCIAL", "SS ", "SEPAE", "URSSAF", "SOZIALVERSICHERUNG", "INPS", "SOCIAL SECURITY")),
    ("TAX_REFUND", ("DEVOLUCION IVA", "DEVOLUCION IRPF", "TAX REFUND", "STEUERERSTATTUNG", "RESTITUTION", "DEVOLUCION HACIENDA", "DEVOLUCION AEAT")),
    ("TAX_PAYMENT", ("AEAT", "HACIENDA", "IMPUESTO", "MODELO 3", "IRPF", "FINANZAMT", "STEUER", "TVA", "IMPOT", "VAT", "TAX OFFICE", "SOCIALE LASTEN")),
    ("LOAN_REPAYMENT", ("AMORTIZACION", "PRESTAMO", "DARLEHEN", "TILGUNG", "KREDIT", "EMPRUNT", "LOAN REPAY", "CUOTA PRESTAMO")),
    ("LOAN_DRAWDOWN", ("DRAWDOWN", "DISPOSICION", "ABRUF", "TRANCHE", "LOAN DRAWDOWN")),
    ("LEASING", ("LEASING", "RENTING", "LEASINGRATE", "CREDIT BAIL")),
    ("INSURANCE", ("SEGURO", "SEGUROS", "VERSICHERUNG", "ASSURANCE", "MAPFRE", "ALLIANZ", "AXA", "GENERALI", "ZURICH")),
    ("CARD_EXPENSES", ("TARJETA", "KARTE", "CARTE", "VISA", "MASTERCARD", "AMEX", "CORPORATE CARD", "KREDITKARTE")),
    ("INTEREST_INCOME", ("INTERESES A FAVOR", "INTERES ACREEDOR", "ZINSGUTSCHRIFT", "INTERETS CREDITEURS", "CREDIT INTEREST")),
    ("INTEREST_EXPENSE", ("INTERESES", "INTERES", "ZINSEN", "INTERETS", "DEBIT INTEREST", "COMISION INTERES")),
    ("CUSTOMER_ADVANCE", ("ANTICIPO", "PROFORMA", "ACOMPTE", "ANZAHLUNG", "ADVANCE PAYMENT", "PAGO A CUENTA")),
    ("CUSTOMER_REFUND", ("DEVOLUCION CLIENTE", "REEMBOLSO CLIENTE", "CUSTOMER REFUND", "RUCKTEST", "REMBOURSEMENT CLIENT")),
    ("SUPPLIER_REFUND", ("DEVOLUCION PROVEEDOR", "SUPPLIER REFUND", "REMBOURSEMENT FOURNISSEUR", "LIEFERANTENGUTSCHRIFT")),
    ("BANK_FEES", ("COMISION", "COMIS", "COMMISSION", "GEBUEHR", "GEBÜHR", "ENTGELT", "FEE", "GASTOS", "CUOTA", "SPESEN", "FRAIS", "CHARGES", "KONTROFUEHRUNG", "MANTENIMIENTO")),
)


def category_for(txn: dict, masters: Masters, settings: Settings, *, own_accounts: dict[str, dict] | None = None) -> str:
    """Category for a bank line that settles no item (masters/categories.csv)."""
    text = _fold(
        " ".join(
            str(txn.get(k) or "")
            for k in ("text", "ref1", "ref2", "remittance_info", "counterparty_name", "bank_subcode")
        )
    )
    amount = int(txn.get("amount_cents") or 0)

    # Our own accounts: internal transfer (same entity) vs intercompany (other entity).
    own_accounts = own_accounts or {}
    counterparty = "".join(ch for ch in str(txn.get("counterparty_account") or "").upper() if ch.isalnum())
    if counterparty and counterparty in own_accounts:
        other = own_accounts[counterparty]
        if other.get("entity") == txn.get("entity"):
            return "INTERNAL_TRANSFER"
        return "INTERCOMPANY"
    if counterparty and counterparty in {a.get("iban") for a in own_accounts.values()}:
        return "INTERNAL_TRANSFER"

    if has_any(text, ("CASH POOL", "POOLING", "BARRIDO", "SWEEP", "ZERO BALANCE")):
        return "CASH_POOL"

    for category, needles in CATEGORY_RULES:
        if has_any(text, needles):
            # A refund needs a credit line; a payment needs a debit line.
            if category == "SUPPLIER_REFUND" and amount < 0:
                continue
            if category == "CUSTOMER_REFUND" and amount > 0:
                continue
            return category

    return settings.classification.default_credit_category if amount > 0 else settings.classification.default_debit_category


__all__ = ["category_for", "exception_for", "is_return", "CATEGORY_RULES"]
