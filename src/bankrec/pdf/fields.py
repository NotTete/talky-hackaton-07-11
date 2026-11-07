"""Fields: from labelled pairs to numbers a rule can trust.

Three jobs, in this order:

1. **normalise** — money to integer cents (comma decimal, space/dot thousands), dates to
   `YYYY-MM-DD`, percentages to floats. One place, so `1 752,08` and `1.752,08` can never
   be two different amounts two modules apart;
2. **resolve** — which label holds which field, per language, by *hint priority* (TOTAL
   beats BASE IMPONIBLE for the total; FECHA beats VENCIMIENTO for the issue date);
3. **check** — net + tax == total, and the printed due date == issue + terms. A failed
   check keeps the fact and lowers confidence; it never silently deletes it.

Fields that matter to reconciliation: document number, issue/due date, currency, net /
tax / total, payment terms, IBAN, and the buyer/seller identity for party resolution.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

CURRENCIES = {
    "EUR", "USD", "GBP", "CHF", "PLN", "MXN", "BRL", "CNY", "ZAR", "MAD", "CAD", "AUD",
    "SEK", "NOK", "DKK", "TRY", "INR", "JPY", "CZK", "HUF", "RON",
}

#: both conventions: 1.752,08 (ES/FR/DE/IT) and 1,752.08 / 1752.08 (EN, and many
#: live entities), plus the rare 1752,08. Deciding by the *last* separator is the only
#: rule that survives both.
MONEY = re.compile(
    r"^\d{1,3}(?:[.\u00a0\u202f ]\d{3})*,\d{2}$"
    r"|^\d{1,3}(?:,\d{3})*\.\d{2}$"
    r"|^\d+[.,]\d{2}$"
)
DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})$|^(\d{4})-(\d{2})-(\d{2})$")
PCT = re.compile(r"^(\d{1,3}(?:[.,]\d{1,2})?)\s?%$")

#: label hints per field, ordered by preference (first match wins at equal purity)
FIELD_HINTS: dict[str, tuple[str, ...]] = {
    # "Nº" folds to "no" (the ordinal indicator has a compatibility decomposition),
    # so both spellings must be hints; "n."/"no." cover the English and German layouts.
    "docno": ("nº", "n°", "no.", "no", "n.", "nr.", "nr", "num", "document", "factura",
              "facture", "invoice", "rechnung", "ref.", "ref"),
    "date": ("fecha", "date", "datum"),
    "due": ("vencimiento", "échéance", "echeance", "fällig", "fellig", "due date", "due"),
    "total": ("total ttc", "total", "gesamt", "importe", "suma", "base imponible",
              "base", "netto", "neto", "subtotal"),
    "po": ("bestellung", "commande", "pedido", "your order", "order", "po "),
    "iban": ("iban", "bic", "cuenta", "konto", "compte"),
}

MONEY_LABELS = ("total", "gesamt", "importe", "suma", "base", "neto", "netto", "subtotal")

TERMS = re.compile(r"(\d{1,3})\s*(?:d[ií]as|days|tage|jours)", re.I)
DISCOUNT = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%.{0,24}?(\d{1,3})\s*(?:d[ií]as|days|tage|jours)", re.I)


def fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def money(value: str) -> int | None:
    """'1 752,08 EUR' / '1,752.08 USD' -> cents. None when it is not money."""
    text = (value or "").strip()
    suffix = re.search(r"\s(%s)$" % "|".join(CURRENCIES), text)
    if suffix:
        text = text[: suffix.start()]
    text = re.sub(r"[\s\u00a0\u202f]|EUR|USD|GBP|CHF", "", text)
    if not MONEY.match(text):
        return None
    if "," in text and "." in text:
        # both separators: whichever comes last is the decimal one
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else text.replace(",", "")
    return int(round(float(text) * 100))


def date(value: str) -> str | None:
    match = DATE.match((value or "").strip())
    if not match:
        return None
    if match.group(4):
        y, m, d = int(match.group(4)), int(match.group(5)), int(match.group(6))
    else:
        d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if y < 100:
            y += 2000
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def percent(value: str) -> float | None:
    match = PCT.match((value or "").strip())
    return float(match.group(1).replace(",", ".")) if match else None


def currency(*values: str) -> str:
    for value in values:
        for token in re.findall(r"\b[A-Z]{3}\b", value or ""):
            if token in CURRENCIES:
                return token
    return ""


@dataclass(slots=True)
class Fields:
    document_number: str = ""
    issue_date: str = ""
    due_date: str = ""
    currency: str = ""
    net: int = 0
    tax: int = 0
    total: int = 0
    terms_days: int = 0
    discount_pct: float = 0.0
    discount_days: int = 0
    iban: str = ""
    buyer: str = ""
    seller: str = ""
    po: str = ""
    labels: dict[str, str] = field(default_factory=dict)   # field -> label that anchored it
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.document_number or self.total)


def _pick(pairs, field_name: str) -> tuple[str, str] | None:
    """Best (label, value) for one field: kind-compatible, hinted, earliest hint wins."""
    hints = FIELD_HINTS[field_name]
    best: tuple[int, int, str, str] | None = None
    for label, value in pairs:
        low = fold(label)
        if field_name == "date" and any(h in low for h in FIELD_HINTS["due"]):
            continue  # "due date" contains "date"
        priority = next((i for i, hint in enumerate(hints) if hint in low), None)
        if priority is None:
            continue
        compatible = (
            money(value) is not None if field_name in {"total"}
            else date(value) is not None if field_name in {"date", "due"}
            else bool(re.match(r"^[A-Za-z0-9][\w./-]{3,}$", value.strip()))
        )
        if not compatible:
            continue
        key = (priority, -len(low), label, value)
        if best is None or key < best:
            best = key
    return (best[2], best[3]) if best else None


def resolve(pairs, text: str, *, currency_code: str = "") -> Fields:
    """From labelled pairs + raw text to the field set the rules consume."""
    out = Fields()
    for name in ("docno", "date", "due", "total", "iban", "po"):
        hit = _pick(pairs, name)
        if not hit:
            continue
        label, value = hit
        out.labels[name] = label
        if name == "docno":
            out.document_number = value.strip()
        elif name == "date":
            out.issue_date = date(value) or ""
        elif name == "due":
            out.due_date = date(value) or ""
        elif name == "iban":
            out.iban = re.sub(r"[^A-Z0-9]", "", value.upper())
        elif name == "po":
            out.po = value.strip()

    # amounts: net / tax / total from the labels, then fall back to the money lines
    money_pairs = [(fold(label), money(value)) for label, value in pairs]
    money_pairs = [(label, value) for label, value in money_pairs if value is not None]
    for label, value in money_pairs:
        if any(word in label for word in ("imponible", "base", "neto", "netto", "ht", "subtotal")) and not out.net:
            out.net = value
        elif any(word in label for word in ("iva", "tva", "mwst", "ust", "tax", "vat")) and not out.tax:
            out.tax = value
        elif any(word in label for word in MONEY_LABELS) and not out.total:
            out.total = value
    if not out.total:
        hit = _pick(pairs, "total")
        if hit:
            out.total = money(hit[1]) or 0

    out.currency = currency(text[:4000], *(value for _, value in pairs[:80])) or currency_code

    terms = TERMS.search(text)
    if terms:
        out.terms_days = int(terms.group(1))
    discount = DISCOUNT.search(text)
    if discount:
        out.discount_pct = float(discount.group(1).replace(",", "."))
        out.discount_days = int(discount.group(2))

    out.checks = {
        "net_plus_tax": bool(out.net and out.tax and out.total and out.net + out.tax == out.total),
        "has_total": out.total > 0,
        "has_docno": bool(out.document_number),
    }
    return out


def seller_buyer(pairs) -> tuple[str, str]:
    """Best-effort identities for party resolution (never used as a hard key)."""
    seller = buyer = ""
    for label, value in pairs:
        low = fold(label)
        if not buyer and low in {"cliente", "client", "kunde", "customer", "billed to"}:
            buyer = value
        if not seller and low in {"proveedor", "fournisseur", "lieferant", "supplier", "vendor"}:
            seller = value
    return seller, buyer


__all__ = ["CURRENCIES", "Fields", "currency", "date", "fold", "money", "percent", "resolve", "seller_buyer"]
