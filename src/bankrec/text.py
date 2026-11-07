"""Turning noisy bank text into something matchable (POLICIES.md §5).

Bank lines are *deliberately* messy: our invoice number arrives without its prefix,
without leading zeros, truncated at 16/35/38 characters, with typos, as the customer's
PO number, or not at all. Counterparty names are truncated or use a trade name.

Start with these four helpers; extend them as you find new noise in TRAIN.
"""

from __future__ import annotations

import re
import unicodedata

_SPLIT = re.compile(r"[\s|,;/+:*()\[\]{}<>\"']+")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")

# Words that look like references but are not (SEPA/EPC vocabulary).
NOISE = {
    "EREF", "NOTPROVIDED", "SVWZ", "ABWA", "RECHNUNG", "RECHNG", "FACTURA", "FACTURE",
    "INVOICE", "FAKTURA", "SEPA", "UEBERWEISUNG", "GUTSCHRIFT", "LASTSCHRIFT", "TRANSFER",
    "PAYMENT", "PAGO", "COBRO", "ADEUDO", "RECIBO", "REMESA", "ABONO", "CARGO", "COMISION",
    "REMITTANCE", "REFERENCE", "ORD", "ORDER", "KUNDE", "CUSTOMER", "MANDAT",
}

# Legal forms that must not drive a name match ("Pooltechnik GmbH" -> POOLTECHNIK).
LEGAL_FORMS = {
    "SL", "SLU", "SA", "SAS", "SARL", "SRL", "SPA", "GMBH", "KG", "CO", "AG", "BV", "NV",
    "LTD", "LIMITED", "LLC", "INC", "CORP", "PLC", "AB", "OY", "AS", "SRO", "GROUP", "GRUPO",
    "HOLDING", "INTERNATIONAL", "ET", "UND", "AND", "OF", "THE", "DE", "DEL", "LA", "EL",
}


def fold(value: str) -> str:
    """Uppercase, accents removed: 'Weißenau' -> 'WEISSENAU'."""
    norm = unicodedata.normalize("NFKD", str(value or ""))
    ascii_ = "".join(ch for ch in norm if not unicodedata.combining(ch))
    return ascii_.upper().replace("ß", "SS")


def alnum(value: str) -> str:
    """'FV22-0519421' -> 'FV220519421' (what you index document numbers by)."""
    return _NON_ALNUM.sub("", fold(value))


def words(value: str) -> list[str]:
    return [w for w in _SPLIT.split(fold(value)) if w]


def name_tokens(value: str) -> set[str]:
    """Distinctive tokens of a party name (no legal form, no 1-2 letter filler)."""
    return {w for w in words(value) if len(w) >= 3 and w not in LEGAL_FORMS and not w.isdigit()}


def name_similarity(a: set[str], b: set[str]) -> float:
    """Coverage of the *smaller* name: bank texts truncate, so recall beats Jaccard."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def reference_tokens(*fields: str, min_len: int = 5) -> set[str]:
    """Candidate document references found in a bank line's free-text fields.

    Keeps tokens of >= min_len that contain at least one digit and are not SEPA
    vocabulary, plus the trailing numeric core of glued tokens
    ('RECHNUNGRE250110905' -> 'RE250110905').
    """
    out: set[str] = set()
    for field in fields:
        for raw in words(field):
            token = alnum(raw)
            if len(token) < min_len or token in NOISE or token.isalpha():
                continue
            out.add(token)
            out.update(part for part in re.findall(r"[A-Z]{0,6}\d{4,}", token) if len(part) >= min_len)
    return out


def ref_variants(token: str) -> set[str]:
    """Spellings of one reference: as-is, without leading zeros, digit core."""
    base = alnum(token)
    if not base:
        return set()
    out = {base}
    digits = "".join(ch for ch in base if ch.isdigit())
    if len(digits) >= 5:
        out.add(digits)
        out.add(digits.lstrip("0") or digits)
    return out


def parse_amount(value: str) -> int | None:
    """'1.210,00' / '1210.00' -> cents. Returns None when it is not a number."""
    s = str(value or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None
