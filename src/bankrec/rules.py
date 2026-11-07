"""POLICIES.md as code: the exception of an item row, the category of a no-item row.

Both are *closed lists* (schema.py). The scorer weights them heavily:
  * a wrong exception costs you the whole transaction (30 % of the score);
  * exception macro-F1 is another 20 %.
When you are not sure, return "" — a wrong code is worse than no code.
"""

from __future__ import annotations

from .data import Masters
from .text import fold

# Keywords are matched against the folded (uppercase, accent-free) bank text.
# Add your own as you explore TRAIN; the docstring of each rule cites the policy.
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # §4: the bank books a wrong movement and then cancels it — BOTH lines.
    ("BANK_REVERSAL", ("ANULACION", "STORNO", "REVERSAL", "RECHARGE", "ANNULATION")),
    # §4: zero-balancing sweeps between participant accounts and the header.
    ("CASH_POOL", ("CASH POOL", "POOLING", "BARRIDO", "SWEEP", "ZERO BALANCE")),
    ("FX_DEAL", ("DIVISA", "DEVISEN", "CAMBIO", "CHANGE", "FOREX", "SPOT")),
    ("PAYROLL", ("NOMINA", "SALARIO", "GEHALT", "LOHN", "SALAIRE", "PAYROLL", "WAGES")),
    ("SOCIAL_SECURITY", ("TGSS", "SEGURIDAD SOCIAL", "URSSAF", "SOZIALVERSICHERUNG")),
    ("TAX_PAYMENT", ("AEAT", "HACIENDA", "IMPUESTO", "IRPF", "FINANZAMT", "STEUER", "TVA", "VAT")),
    ("TAX_REFUND", ("DEVOLUCION IVA", "DEVOLUCION IRPF", "TAX REFUND", "STEUERERSTATTUNG")),
    ("LOAN_REPAYMENT", ("AMORTIZACION", "PRESTAMO", "DARLEHEN", "KREDIT", "EMPRUNT")),
    ("LEASING", ("LEASING", "RENTING")),
    ("INSURANCE", ("SEGURO", "VERSICHERUNG", "ASSURANCE", "MAPFRE", "ALLIANZ", "AXA")),
    ("CARD_EXPENSES", ("TARJETA", "KARTE", "CARTE", "VISA", "MASTERCARD", "AMEX")),
    ("INTEREST_INCOME", ("INTERESES A FAVOR", "ZINSGUTSCHRIFT", "CREDIT INTEREST")),
    ("INTEREST_EXPENSE", ("INTERESES", "ZINSEN", "INTERETS", "DEBIT INTEREST")),
    # §4: a proforma is not an invoice; the customer pays before the invoice exists.
    ("CUSTOMER_ADVANCE", ("ANTICIPO", "PROFORMA", "ACOMPTE", "ANZAHLUNG")),
    ("CUSTOMER_REFUND", ("DEVOLUCION CLIENTE", "REEMBOLSO CLIENTE", "CUSTOMER REFUND")),
    ("SUPPLIER_REFUND", ("DEVOLUCION PROVEEDOR", "SUPPLIER REFUND", "LIEFERANTENGUTSCHRIFT")),
    ("BANK_FEES", ("COMISION", "GEBUEHR", "ENTGELT", " FEE", "GASTOS", "CUOTA", "FRAIS", "CHARGES")),
]


def category_for(txn: dict, masters: Masters) -> str:
    """Category for a bank line that settles no item (POLICIES.md §4).

    Extend in this order:
      1. our own IBANs (masters.own_accounts) -> INTERNAL_TRANSFER / INTERCOMPANY;
      2. the keyword table below;
      3. a per-bank code table (bank_code / bank_subcode) — banks are consistent;
      4. fallback by sign: credit -> UNIDENTIFIED, debit -> BANK_FEES.
    """
    text = fold(
        " ".join(
            str(txn.get(k) or "")
            for k in ("text", "ref1", "ref2", "remittance_info", "counterparty_name", "bank_subcode")
        )
    )
    accounts = masters.own_accounts
    counterparty = "".join(ch for ch in str(txn.get("counterparty_account") or "").upper() if ch.isalnum())
    if counterparty in accounts:
        return "INTERNAL_TRANSFER" if accounts[counterparty]["entity"] == txn.get("entity") else "INTERCOMPANY"

    amount = int(txn.get("amount_cents") or 0)
    for category, needles in CATEGORY_KEYWORDS:
        if not any(n in text for n in needles):
            continue
        # a refund is a credit, a payment is a debit (sign check saves false positives)
        if category == "SUPPLIER_REFUND" and amount < 0:
            continue
        if category == "CUSTOMER_REFUND" and amount > 0:
            continue
        return category

    return "UNIDENTIFIED" if amount > 0 else "BANK_FEES"


def exception_for(
    txn: dict, item: dict, applied_cents: int, remaining_cents: int, masters: Masters, reason: str = ""
) -> str:
    """Exception for one (bank line, item) row (POLICIES.md §3).

    `applied_cents` is what this line puts against the item; `remaining_cents` is what
    is still open afterwards. The full rule set:

      PARTIAL              remaining > 0 and no specific reason below
      SHORT_DISPUTE        remaining > 0 and the advice states a reason
      SHORT_DISCOUNT       within terms (+3 days grace) and cash = total - discount
      SHORT_WITHHOLDING    cash = total - 1.25 / 1.5 / 10 %
      SHORT_BANK_CHARGES   shortfall <= 60 EUR and <= 2 % of the item
      OVERPAYMENT          cash > open amount (the invoice clears, excess stays on account)
      FX_DIFFERENCE        bank line currency != item currency
      DUPLICATE_PAYMENT    the item was already settled and is paid again
      RETURNED             a collection/payment comes back and re-opens the item
      ""                   paid as documented (also the last instalment / credit notes)

    Start simple; each branch you add is worth a few points in TRAIN.
    """
    open_cents = applied_cents + remaining_cents
    tolerance = max(1, int(round(open_cents * 0.005)))

    if remaining_cents > tolerance:
        return "SHORT_DISPUTE" if reason else "PARTIAL"

    item_currency = item.get("currency")
    line_currency = txn.get("currency")
    if item_currency and line_currency and item_currency != line_currency:
        return "FX_DIFFERENCE"  # §3: replaces discount/withholding/charges, keeps PARTIAL

    if applied_cents > open_cents + tolerance:
        return "OVERPAYMENT"

    # TODO: SHORT_DISCOUNT   -> masters.terms[item["payment_terms"]] + booking_date
    # TODO: SHORT_WITHHOLDING-> schema.WITHHOLDING_RATES against item["total_cents"]
    # TODO: SHORT_BANK_CHARGES -> schema.BANK_CHARGE_MAX_EUR / BANK_CHARGE_MAX_PCT
    return ""
