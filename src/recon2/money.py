"""Money parsing: strings -> int cents, never through float (CLAUDE.md §3)."""

from __future__ import annotations

import re

_SPACES = re.compile(r"[\s   '’_]")
_DIGITS = re.compile(r"^\d+$")


def _int_units(s: str) -> int:
    if not _DIGITS.match(s):
        raise ValueError(f"not a number: {s!r}")
    return int(s)


def money(s: str) -> int:
    """Locale-agnostic amount -> signed cents.

    The last '.' or ',' followed by exactly two (or one) digits at the end is the decimal
    mark; every other separator is a thousands separator.  Without a decimal tail the
    value must be unambiguous (no separators, or several groups of three / mixed marks).
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("empty amount")
    neg = False
    if raw.startswith("(") and raw.endswith(")"):
        neg, raw = True, raw[1:-1].strip()
    if raw.startswith("-") or raw.startswith("−"):
        neg, raw = True, raw[1:]
    elif raw.endswith("-"):
        neg, raw = True, raw[:-1]
    elif raw.startswith("+"):
        raw = raw[1:]
    raw = _SPACES.sub("", raw)
    if not raw:
        raise ValueError(f"not a number: {s!r}")
    pos = max(raw.rfind("."), raw.rfind(","))
    if pos >= 0 and 1 <= len(raw) - pos - 1 <= 2:
        head, tail = raw[:pos], raw[pos + 1 :]
        head = head.replace(".", "").replace(",", "") or "0"
        val = _int_units(head) * 100 + _int_units(tail.ljust(2, "0"))
    elif pos < 0:
        val = _int_units(raw) * 100
    else:
        groups = re.split(r"[.,]", raw)
        marks = set(re.findall(r"[.,]", raw))
        ok = all(len(g) == 3 for g in groups[1:]) and 1 <= len(groups[0]) <= 3
        if not ok or (len(groups) == 2 and len(marks) == 1):
            raise ValueError(f"ambiguous amount: {s!r}")
        val = _int_units("".join(groups)) * 100
    return -val if neg else val


def cents(s: str | None) -> int:
    """Plain dot-decimal CSV amount ('-1487.60', '12', '3.5') -> int cents; '' -> 0."""
    if s is None:
        return 0
    s = s.strip()
    if not s:
        return 0
    neg = s.startswith("-")
    if neg or s.startswith("+"):
        s = s[1:]
    whole, _, frac = s.partition(".")
    if frac and not frac.isdigit() or whole and not whole.isdigit():
        raise ValueError(f"not a dot-decimal amount: {s!r}")
    val = int(whole or "0") * 100 + int((frac + "00")[:2])
    if len(frac) > 2 and frac[2] >= "5":  # half-up on the magnitude
        val += 1
    return -val if neg else val
