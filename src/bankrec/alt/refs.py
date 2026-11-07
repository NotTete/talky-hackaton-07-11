"""Pulling every document reference a bank line *could* be naming out of its text.

`text.reference_tokens` already yields the obvious tokens. Two things it cannot do,
and both of them are worth thousands of pairs on the sample:

1. **Continuation lists.** A 1:N receipt names its documents once and then abbreviates:

       SVWZ+RE RE250106232 106234 106402 106390 106918 105438 107372

   Only the first token is a whole invoice number; the rest share its prefix and are
   printed as the differing tail. Expanding them turns one matched item into eight.

2. **Truncation.** Banks cut free text at 16/35/38 characters, so the *last* token of a
   line is routinely a prefix of a real document number ("FRAS FV25-02", "PAGO FRA.
   FV25-0"). Treating it as a prefix query instead of an exact key is the difference
   between "no reference" and "this customer's FV25-02xxxxx invoices".

Everything returned here is a *hypothesis*; `ledger.DocIndex` decides whether a token
resolves to an item, and the engine decides whether the resolution is good enough to use.
"""

from __future__ import annotations

import re

from ..text import NOISE, alnum, fold

# Tokens that are structurally references of *ours* rather than a document: the bank's
# own movement id, a SEPA end-to-end id, a mandate. They are matched by the operation
# resolver (ops.py), never by the document index.
_OPERATIONAL = re.compile(r"^(PAY|REM|DEV|LO|NONREF|NOTPROVIDED|VTM|PR\d)")

_WORD = re.compile(r"[A-Z0-9][A-Z0-9/.\-]*")

# A reference needs letters+digits or >= 6 digits; "3" and "EUR" are not references.
_REFLIKE = re.compile(r"^(?=.*\d)[A-Z0-9]{5,}$")


def _split(blob: str) -> list[str]:
    return _WORD.findall(fold(blob))


def _is_bare_tail(token: str) -> bool:
    """A continuation token: pure digits, 4-8 of them (an invoice tail, not an amount)."""
    return token.isdigit() and 4 <= len(token) <= 8


def extract(*fields: str) -> list[str]:
    """Ordered, de-duplicated reference hypotheses for one bank line.

    Order matters: the engine prefers earlier tokens when two resolve to different
    items, and a deterministic order is what makes the build reproducible (CLAUDE.md
    §12).
    """
    out: list[str] = []
    seen: set[str] = set()

    def push(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            out.append(token)

    for field in fields:
        anchor = ""  # the last full reference seen, for continuation expansion
        for raw in _split(str(field or "")):
            token = alnum(raw)
            if not token or token in NOISE:
                continue
            if _REFLIKE.match(token) and not token.isdigit():
                push(token)
                anchor = token
                # glued vocabulary: 'RECHNUNGRE250110905' -> 'RE250110905'
                for part in re.findall(r"[A-Z]{1,4}\d{5,}", token):
                    if part != token:
                        push(part)
                continue
            if _is_bare_tail(token):
                if anchor and len(anchor) > len(token):
                    push(anchor[: len(anchor) - len(token)] + token)
                # a bare number can also be the whole document number (AP suppliers
                # number their invoices 25001457, 2025-0217, ...).
                if len(token) >= 6:
                    push(token)
                    anchor = anchor or token
                continue
            if token.isdigit() and len(token) > 8:
                push(token)
    return out


def operational(*fields: str) -> list[str]:
    """Tokens that look like an *operation* id (batch, run, settlement, mandate, e2e).

    Kept separate from `extract` because they are joined against the document folders,
    not against the item ledger, and because a batch id in the text must never be tried
    as an invoice number.
    """
    out: list[str] = []
    seen: set[str] = set()
    for field in fields:
        for raw in _split(str(field or "")):
            token = alnum(raw)
            if len(token) >= 8 and token not in seen and any(ch.isdigit() for ch in token):
                seen.add(token)
                out.append(token)
    return out


def digits(token: str) -> str:
    return "".join(ch for ch in token if ch.isdigit())


def variants(token: str) -> list[str]:
    """Spellings of one reference, most specific first."""
    base = alnum(token)
    if not base:
        return []
    out = [base]
    core = digits(base)
    if len(core) >= 5:
        if core != base:
            out.append(core)
        stripped = core.lstrip("0")
        if stripped and stripped != core:
            out.append(stripped)
    return out


__all__ = ["extract", "operational", "variants", "digits"]
