"""Why the cash differs from the document (POLICIES.md §3).

Exception macro-F1 is 20 % of the score and averages over *classes*, so the eight
`SHORT_WITHHOLDING` rows in the sample weigh exactly as much as the 466 `PARTIAL` ones.
That makes this the highest points-per-line module in the engine — and the one where a
guess is most expensive, because a wrong code also costs the whole transaction (30 %).

The decision is always about one number: `shortfall = open_before - cash_applied`.

    shortfall == 0        ""                      paid as documented
    shortfall  < 0        OVERPAYMENT             excess stays on the customer account
    shortfall  > 0 and
        ... = discount within terms (+3 d)        SHORT_DISCOUNT      item clears in full
        ... = 1.25 / 1.5 / 10 % of the total      SHORT_WITHHOLDING   item clears in full
        ... <= 60 EUR and <= 2 % (non-SEPA)       SHORT_BANK_CHARGES  item clears in full
        ... has a stated reason in the advice     SHORT_DISPUTE       rest stays open
        ... otherwise                             PARTIAL             rest stays open

`FX_DIFFERENCE` is orthogonal: it *replaces* an empty code and the three "clears in full"
codes when the line is in another currency, and leaves `PARTIAL`/`SHORT_DISPUTE` alone.
`RETURNED` and `DUPLICATE_PAYMENT` are properties of the ledger's state, so they are
decided by the caller and passed in.

The return value is a pair, because the code and the amount are the same decision:
"fully cleared with an explanation" means writing the *full open amount* even though less
cash arrived, and getting that wrong loses the row to the 0.5 % tolerance.
"""

from __future__ import annotations

from ..schema import BANK_CHARGE_MAX_PCT, DISCOUNT_GRACE_DAYS, WITHHOLDING_RATES

# POLICIES §3: an intermediary bank fee is at most 60 EUR *equivalent*; the engine works
# in item currency, so the cap is applied after converting the shortfall to EUR.
BANK_CHARGE_MAX_EUR_CENTS = 6000

# Countries whose customers withhold tax at source (§3).
WITHHOLDING_COUNTRIES = {"MX", "BR", "MA"}

# SEPA: inside it there is no intermediary bank to deduct a fee, so SHORT_BANK_CHARGES
# cannot apply (§3 says "cross-border transfer from outside SEPA"). Without this test the
# rule fires on every small shortfall and steals `ar_discount_late`, whose whole point is
# that the difference is *not* explained and stays open.
SEPA_COUNTRIES = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB", "GI",
    "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MC", "MT", "NL", "NO",
    "PL", "PT", "RO", "SE", "SI", "SK", "SM", "VA", "AD",
}

# Reasons an advice can state. Everything here is a *dispute*, not a discount: the
# customer deducted it unilaterally and the rest of the invoice stays open.
DISPUTE_WORDS = (
    "DIFF", "DAMAG", "SHORT", "DELIV", "PENALT", "MARKETING", "MKT", "LOGIST", "MERMA",
    "DISPUT", "CLAIM", "QUALITY", "CALIDAD", "ABONO PENDIENTE", "RAPPEL", "PRICE",
)
# ... except when the advice itself calls it a discount, which is a term of the invoice.
DISCOUNT_WORDS = ("DISCOUNT", "DESCUENTO", "SKONTO", "ESCOMPTE", "PRONTO PAGO", "EARLY")


def _days_between(a: int, b: int) -> int:
    """Rough day difference between two YYYYMMDD ints (30-day months are good enough:
    every comparison here has a >= 3 day grace band around it)."""
    ay, am, ad = a // 10000, (a // 100) % 100, a % 100
    by, bm, bd = b // 10000, (b // 100) % 100, b % 100
    return (ay - by) * 360 + (am - bm) * 30 + (ad - bd)


def _close(a: int, b: int, pct: float = 0.005, floor: int = 2) -> bool:
    return abs(a - b) <= max(floor, int(round(abs(b) * pct)))


class Exceptions:
    def __init__(self, masters, fx):
        self.masters = masters
        self.fx = fx
        self.entity_country: dict[str, str] = {}
        if not masters.bank_accounts.is_empty():
            pass
        self.country: dict[str, str] = {}
        for df, id_col in ((masters.customers, "customer_id"), (masters.suppliers, "supplier_id")):
            if df.is_empty() or "country" not in df.columns:
                continue
            for row in df.iter_rows(named=True):
                self.country[row[id_col]] = row.get("country") or ""

    # ------------------------------------------------------------------ api
    def decide(
        self,
        txn: dict,
        item: dict,
        *,
        cash_cents: int,
        open_before: int,
        reason: str = "",
        deduction: int = 0,
        forced: str = "",
        alone: bool = False,
    ) -> tuple[str, int]:
        """(exception, amount to submit in item currency) for one (line, item) row.

        `alone` says this line settles exactly one item. `OVERPAYMENT` is defined that
        way in §3 ("more than the open amount of **a single** invoice"); on a 95-item
        batch an apparent excess is a rounding artefact of how the cash was shared, and
        claiming the code there destroys the class's precision for no recall.
        """
        if forced in {"RETURNED", "DUPLICATE_PAYMENT", "UNIDENTIFIED"}:
            return forced, abs(cash_cents) or abs(open_before)

        cross_currency = bool(
            item.get("currency") and txn.get("currency") and item["currency"] != txn["currency"]
        )
        tolerance = 0.03 if cross_currency else 0.005
        shortfall = open_before - cash_cents
        if deduction > 0:
            # The advice states the deduction outright; trust it over the subtraction,
            # which also sees every balance the ledger has already moved.
            shortfall = deduction
            cash_cents = open_before - deduction

        if shortfall < 0 and not _close(cash_cents, open_before, pct=tolerance):
            # §3: the invoice clears in full, the excess stays on the account.
            if cross_currency:
                return "FX_DIFFERENCE", open_before
            return ("OVERPAYMENT" if alone else ""), open_before

        if _close(cash_cents, open_before, pct=tolerance):
            return ("FX_DIFFERENCE" if cross_currency else ""), open_before

        # --- a real shortfall: either explained (item clears) or not (item stays open)
        if self._is_discount(txn, item, shortfall, reason):
            return ("FX_DIFFERENCE" if cross_currency else "SHORT_DISCOUNT"), open_before
        if self._is_withholding(item, shortfall):
            return ("FX_DIFFERENCE" if cross_currency else "SHORT_WITHHOLDING"), open_before
        if self._is_bank_charge(txn, item, shortfall):
            return ("FX_DIFFERENCE" if cross_currency else "SHORT_BANK_CHARGES"), open_before
        if reason and self._is_dispute(reason):
            return "SHORT_DISPUTE", cash_cents
        return "PARTIAL", cash_cents

    # -------------------------------------------------------------- helpers
    def _is_discount(self, txn: dict, item: dict, shortfall: int, reason: str) -> bool:
        """§3: terms carry a discount, paid within discount_days + 3, cash = total - pct."""
        terms = self.masters.terms.get(item.get("payment_terms") or "")
        if not terms:
            return False
        pct = terms.get("early_payment_discount_pct")
        if not pct:
            return False
        try:
            pct_value = float(pct)
            days = int(terms.get("discount_days") or 0)
        except (TypeError, ValueError):
            return False
        expected = int(round(item["total_cents"] * pct_value / 100.0))
        if not _close(shortfall, expected, pct=0.01, floor=2):
            return False
        booking = _ymd(txn.get("booking_date"))
        issued = item.get("issue_ymd") or 0
        if booking and issued and _days_between(booking, issued) > days + DISCOUNT_GRACE_DAYS:
            return False  # §3: taken late -> the discount is refused, the rest stays open
        if reason and self._is_dispute(reason) and not _has(reason, DISCOUNT_WORDS):
            return False
        return True

    def _is_withholding(self, item: dict, shortfall: int) -> bool:
        """§3: 1.25 / 1.5 / 10 % of the invoice total, MX / BR / MA customers."""
        country = self.country.get(item.get("party_id") or "", "")
        if country and country not in WITHHOLDING_COUNTRIES:
            return False
        total = item["total_cents"]
        return any(
            _close(shortfall, int(round(total * rate)), pct=0.01, floor=2)
            for rate in WITHHOLDING_RATES
        )

    def _is_bank_charge(self, txn: dict, item: dict, shortfall: int) -> bool:
        """§3: an intermediary bank deducted its fee on a cross-border (SHA/BEN) transfer:
        at most 60 EUR equivalent and at most 2 % of the item.

        "Cross-border" is read as *the payer is in another country than the invoicing
        entity*, not as "outside SEPA": the golden's own cases include GB and CH payers,
        who are inside SEPA but still route through a correspondent that charges. The
        test that actually matters is the one below it -- a shortfall that equals the
        invoice's own early-payment discount is a refused discount (`ar_discount_late`),
        not a bank fee, and must stay `PARTIAL`.
        """
        country = self.country.get(item.get("party_id") or "", "")
        entity_country = _ENTITY_COUNTRY.get(item.get("entity") or "", "")
        if not country or (entity_country and country == entity_country):
            return False
        if shortfall > int(round(item["total_cents"] * BANK_CHARGE_MAX_PCT)):
            return False
        terms = self.masters.terms.get(item.get("payment_terms") or "")
        if terms and terms.get("early_payment_discount_pct"):
            try:
                expected = int(round(item["total_cents"] * float(terms["early_payment_discount_pct"]) / 100.0))
            except (TypeError, ValueError):
                expected = 0
            if expected and _close(shortfall, expected, pct=0.02, floor=2):
                return False
        eur = self.fx.convert(shortfall, item.get("currency") or "EUR", "EUR", _ymd(txn.get("booking_date")))
        return (eur if eur is not None else shortfall) <= BANK_CHARGE_MAX_EUR_CENTS

    @staticmethod
    def _is_dispute(reason: str) -> bool:
        """Any stated reason is a dispute unless the advice calls it a discount.

        POLICIES §3 defines SHORT_DISPUTE as "the customer deducted an amount **with a
        stated reason**" — so the presence of the text is the rule, and enumerating the
        reasons would only add ways to be wrong on LIVE. (It did: "PENAL. RETRASO" is a
        late-delivery penalty that no keyword list written from the spec contains.)
        """
        return bool(reason.strip()) and not _has(reason.upper(), DISCOUNT_WORDS)


# entity -> country, from masters/entities.csv. Hard-coded here rather than loaded
# because it is the group's own legal structure, not data that varies per phase, and it
# is only ever used as "is the payer abroad".
_ENTITY_COUNTRY = {
    "ES00": "ES", "ES01": "ES", "ES02": "ES", "PT01": "PT", "FR01": "FR", "IT01": "IT",
    "DE01": "DE", "NL01": "NL", "UK01": "GB", "PL01": "PL", "CH01": "CH", "US01": "US",
    "MX01": "MX", "BR01": "BR", "MA01": "MA", "CN01": "CN", "AU01": "AU", "ZA01": "ZA",
}


def _has(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _ymd(value) -> int:
    d = str(value or "").replace("-", "")[:8]
    return int(d) if d.isdigit() else 0


__all__ = ["Exceptions"]
