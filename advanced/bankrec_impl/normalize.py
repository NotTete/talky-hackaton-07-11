"""Text normalisation: references, tokens, party names.

Bank texts are noisy by design (POLICIES.md §5): truncated names, numbers without
prefix or leading zeros, typos, transposed digits, PO numbers instead of ours.
These helpers are deliberately cheap and deterministic so they can run over ~2M
bank lines.
"""

from __future__ import annotations

import re
import unicodedata

NON_ALNUM = re.compile(r"[^A-Z0-9]+")
DIGITS = re.compile(r"\d+")
SPACES = re.compile(r"\s+")

# Legal-form / filler tokens that must not drive a name match.
NAME_NOISE = {
    "SL", "SLU", "SLNE", "SA", "SAS", "SAL", "SARL", "SRL", "SPA", "SOCIEDAD", "LIMITADA",
    "GMBH", "GMBHCH", "KG", "CO", "AG", "BV", "NV", "AB", "OY", "AS", "A", "S", "LTD",
    "LIMITED", "LLC", "INC", "CORP", "PLC", "SA", "SP", "ZOO", "SPOLKA", "SRO",
    "S", "A", "R", "L", "Y", "CIA", "COMPANY", "GROUP", "GRUPO", "HOLDING", "INTERNATIONAL",
    "DE", "DEL", "LA", "EL", "LOS", "LAS", "DER", "DIE", "DAS", "DES", "LE", "LES",
    "ET", "UND", "AND", "OF", "THE", "SOCIETE", "FR", "SARL", "EURL",
}

# Free-text noise that is not a document reference (SEPA/EPC vocabulary).
REF_NOISE = {
    "EREF", "NOTPROVIDED", "SVWZ", "ABWA", "RECHNUNG", "RECHNG", "FAKTURA", "FACTURA",
    "FACTURE", "INVOICE", "FATTURA", "RECHNUNGSNR", "BELEG", "KUNDE", "CUSTOMER",
    "VORGANG", "MANDAT", "SEPA", "UEBERWEISUNG", "GUTSCHRIFT", "LASTSCHRIFT",
    "UEBERW", "DAUERAUFTRAG", "SAMMEL", "TRANSFER", "TRF", "PAYMENT", "PAIEMENT",
    "PAGO", "COBRO", "ADEUDO", "RECIBO", "REMESA", "ABONO", "CARGO", "COMISION",
}

# Tokens that legitimately look like references but are not ours.
COMMON_WORDS = REF_NOISE | NAME_NOISE


def strip_accents(value: str) -> str:
    norm = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in norm if not unicodedata.combining(ch))


def upper_ascii(value: str) -> str:
    return strip_accents(value or "").upper()


def norm_alnum(value: str) -> str:
    """'FV22-0519421' -> 'FV220519421'."""
    return NON_ALNUM.sub("", upper_ascii(value))


def norm_text(value: str) -> str:
    """Uppercase, accents removed, punctuation collapsed; keeps word boundaries."""
    return SPACES.sub(" ", NON_ALNUM.sub(" ", upper_ascii(value))).strip()


def tokens(value: str) -> list[str]:
    return [t for t in norm_text(value).split(" ") if t]


def ref_variants(value: str) -> set[str]:
    """All plausible spellings of one reference token.

    Covers the documented noise: our number without prefix, without leading zeros,
    truncated at 16/35/38 characters, and the bare digit run.
    """
    base = norm_alnum(value)
    if not base:
        return set()
    out = {base}
    if len(base) > 8:
        # 'RE250110905' / 'FV220519421' -> numeric core, useful for typo tolerance
        digits = "".join(DIGITS.findall(base))
        if len(digits) >= 5:
            out.add(digits.lstrip("0") or digits)
    return out


def reference_tokens(*fields: str, min_len: int = 5) -> set[str]:
    """Candidate document references found in the free-text fields of a bank line."""
    out: set[str] = set()
    for field in fields:
        for raw in re.split(r"[\s|,;/+:*()\[\]{}<>\"']+", upper_ascii(field or "")):
            tok = norm_alnum(raw)
            if len(tok) < min_len or tok in COMMON_WORDS:
                continue
            if tok.isalpha():  # words, not references
                continue
            out.add(tok)
            # Long tokens often glue words: 'RECHNUNGRE250110905' -> trailing ref
            for part in re.findall(r"[A-Z]{0,6}\d{4,}", tok):
                if len(part) >= min_len:
                    out.add(part)
    return out


def name_tokens(value: str) -> set[str]:
    """Distinctive tokens of a party name (no legal form, no stop words)."""
    out = set()
    for tok in tokens(value):
        if len(tok) < 3 or tok in NAME_NOISE or tok.isdigit():
            continue
        out.add(tok)
    return out


def name_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    # Coverage of the smaller name: bank texts truncate, so recall matters more
    # than Jaccard.
    return inter / min(len(a), len(b))


def parse_amount(value: str) -> int | None:
    """'1.210,00' / '1210.00' / '1,210.00' -> cents."""
    s = (value or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # European: 1.210,00
            s = s.replace(".", "").replace(",", ".")
        else:  # Anglo: 1,210.00
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else s.replace(",", "")
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None
