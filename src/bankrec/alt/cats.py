"""The category of a line that settles no item (POLICIES.md §4).

Three sources of evidence, in order of how much they can be trusted:

1. **Our own IBANs.** A counterparty that is one of the group's accounts is an internal
   transfer (same entity) or intercompany (another entity) by definition, and the cash
   pool header is the same test plus the pooling purpose in `bank_accounts.csv`.
2. **The bank's own transaction code.** `bank_code`/`bank_subcode` are undocumented but
   perfectly consistent per bank: `NCHG`, `17/310` and `ACMT/MCOP/CHRG` are *always*
   fees, `CAMT/ACCB/ZABA` and `99/867` are *always* the zero-balancing sweep. The map is
   harvested from TRAIN's golden rather than guessed (`scripts/harvest_codes.py`), so it
   is evidence rather than a hunch, and an unseen code simply falls through.
3. **Keywords**, last, because they are the only source that can fire on a line whose
   code says something else.

The fallback is deliberately asymmetric: an unexplained **credit** is `UNIDENTIFIED`
(POLICIES §3 says so outright — some unidentified receipts even carry the exact amount of
an unrelated invoice, and forcing a match there is a double loss), while an unexplained
**debit** is a fee, which is what the long tail of small debits actually is.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..text import fold

CODE_MAP_PATH = Path(__file__).resolve().parents[3] / "config" / "bank_codes.json"

# (category, needles) — ordered: the first hit wins, so specific beats generic.
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("BANK_REVERSAL", ("ANULACION", "ANULACIÓN", "OPERACION INDEBIDA", "STORNO", "REVERSAL",
                       "ANNULATION", "EXTORNO", "RETROCESSION")),
    ("CASH_POOL", ("CASH POOL", "POOLING", "BARRIDO", "SWEEP", "ZERO BALANCE", "ZABA",
                   "NIVELACION", "CONCENTRACION")),
    ("FX_DEAL", ("COMPRA DIVISA", "VENTA DIVISA", "DEVISEN", "FX DEAL", "FOREX", "SPOT ",
                 "OPERACION DE CAMBIO")),
    ("PAYROLL", ("NOMINA", "NÓMINA", "SALARIO", "GEHALT", "LOHN", "SALAIRE", "PAYROLL", "WAGES")),
    ("SOCIAL_SECURITY", ("TGSS", "SEGURIDAD SOCIAL", "URSSAF", "SOZIALVERSICHERUNG",
                         "NATIONAL INSURANCE", "INPS")),
    ("TAX_REFUND", ("DEVOLUCION IVA", "DEVOLUCION IRPF", "TAX REFUND", "STEUERERSTATTUNG",
                    "REMBOURSEMENT TVA")),
    ("TAX_PAYMENT", ("AEAT", "HACIENDA", "IMPUESTO", "IRPF", "FINANZAMT", "STEUER", "TVA ",
                     "VAT ", "MODELO 3", "MODELO 1", "HMRC")),
    ("LOAN_DRAWDOWN", ("DISPOSICION PRESTAMO", "DRAWDOWN", "DISPOSICIÓN PRÉSTAMO")),
    ("LOAN_REPAYMENT", ("AMORTIZACION", "AMORTIZACIÓN", "PRESTAMO", "PRÉSTAMO", "DARLEHEN",
                        "KREDIT", "EMPRUNT", "LOAN")),
    ("LEASING", ("LEASING", "RENTING", "ARRENDAMIENTO FINANCIERO")),
    ("INSURANCE", ("SEGURO", "VERSICHERUNG", "ASSURANCE", "POLIZA", "PÓLIZA")),
    ("CARD_EXPENSES", ("LIQUIDACION TARJETA", "EXTRACTO TARJETA", "KARTENABRECHNUNG",
                       "CORPORATE CARD", "RELEVE CARTE")),
    ("INTEREST_INCOME", ("INTERESES A FAVOR", "INTERESES ACREEDORES", "ZINSGUTSCHRIFT",
                         "CREDIT INTEREST", "ABONO INTERESES")),
    ("INTEREST_EXPENSE", ("INTERESES DEUDORES", "ZINSEN", "INTERETS", "DEBIT INTEREST",
                          "INTERESES")),
    ("CUSTOMER_ADVANCE", ("ANTICIPO", "PROFORMA", "ACOMPTE", "ANZAHLUNG", "ADVANCE PAYMENT")),
    ("CUSTOMER_REFUND", ("DEVOLUCION CLIENTE", "REEMBOLSO CLIENTE", "CUSTOMER REFUND",
                         "DEVOLUCION DUPLICADO", "REINTEGRO CLIENTE")),
    ("SUPPLIER_REFUND", ("DEVOLUCION PROVEEDOR", "SUPPLIER REFUND", "LIEFERANTENGUTSCHRIFT",
                         "REINTEGRO PROVEEDOR")),
    ("BANK_FEES", ("COMISION", "COMISIÓN", "GEBUEHR", "GEBÜHR", "ENTGELT", " FEE", "FEES",
                   "GASTOS", "CUOTA", "FRAIS", "CHARGES", "PORTES", "CORRETAJE")),
]


def _load_code_map() -> dict[str, str]:
    if not CODE_MAP_PATH.exists():
        return {}
    try:
        return json.loads(CODE_MAP_PATH.read_text(encoding="utf-8")).get("codes", {})
    except (ValueError, OSError):
        return {}


class Categories:
    def __init__(self, masters):
        self.masters = masters
        self.codes = _load_code_map()
        self.pool_accounts = {
            iban
            for iban, row in masters.own_accounts.items()
            if "pool" in str(row.get("purpose") or "").lower()
        }

    def of(self, txn: dict) -> str:
        counterparty = "".join(ch for ch in str(txn.get("counterparty_account") or "") if ch.isalnum()).upper()
        own = self.masters.own_accounts.get(counterparty)
        amount = int(txn.get("amount_cents") or 0)
        text = fold(
            " ".join(str(txn.get(k) or "") for k in ("text", "ref1", "ref2", "remittance_info", "counterparty_name"))
        )

        if own is not None:
            if counterparty in self.pool_accounts or "POOL" in fold(str(own.get("purpose") or "")):
                if _hit(text, ("POOL", "BARRIDO", "SWEEP", "ZABA", "NIVELACION")):
                    return "CASH_POOL"
            return "INTERNAL_TRANSFER" if own.get("entity") == txn.get("entity") else "INTERCOMPANY"

        code = self._code(txn)
        by_code = self.codes.get(code) or self.codes.get(code.split("/")[0])
        if by_code and self._sign_ok(by_code, amount):
            return by_code

        for category, needles in KEYWORDS:
            if _hit(text, needles) and self._sign_ok(category, amount):
                return category

        return "UNIDENTIFIED" if amount > 0 else "BANK_FEES"

    @staticmethod
    def _code(txn: dict) -> str:
        code = str(txn.get("bank_code") or "").strip()
        sub = str(txn.get("bank_subcode") or "").strip()
        return f"{code}/{sub}" if sub and sub != "None" else code

    @staticmethod
    def _sign_ok(category: str, amount: int) -> bool:
        """A refund is a credit, a payment is a debit. One sign test kills most false
        positives of both the code map and the keywords."""
        credits = {"CUSTOMER_ADVANCE", "SUPPLIER_REFUND", "TAX_REFUND", "INTEREST_INCOME",
                   "LOAN_DRAWDOWN", "UNIDENTIFIED"}
        debits = {"CUSTOMER_REFUND", "PAYROLL", "SOCIAL_SECURITY", "TAX_PAYMENT", "LEASING",
                  "INSURANCE", "LOAN_REPAYMENT", "INTEREST_EXPENSE", "CARD_EXPENSES"}
        if category in credits:
            return amount > 0
        if category in debits:
            return amount < 0
        return True


def _hit(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


__all__ = ["Categories"]
