"""Treasury classifier (DESIGN §3): final category for own-group money movements, by text regex only.

TRAIN: 19,244/19,250 treasury lines, 0 false positives.  The own-IBAN rule is dead (counterparty
account is NULL on every treasury line).  Never match bare POOL/SWEEP/PRESTAMO/TRASPASO: on pool
accounts most "POOL" lines are customer receipts ("POOL SUPPLY ..."), bare PRESTAMO is a loan.
INTERCOMPANY is tested before anything loan-like.
"""

from __future__ import annotations

import re
import unicodedata

from recon2.model import Line

_ORDER = (
    ("CASH_POOL", re.compile(
        r"CASH CONCENTRATION|ZBA SWEEP TO HEADER|TRASPASO CASH POOLING|TRASPASO ENTRE CUENTAS ES00|TRASPASO PROPIO")),
    ("INTERNAL_TRANSFER", re.compile(r"TRASPASO (?:A|DE) CTA \d{4}")),
    ("FX_DEAL", re.compile(r"FX DEAL \d{10}|(?:COMPRA|VENTA) DIVISA [A-Z]{3} ")),
    ("INTERCOMPANY", re.compile(
        r"\bIC VALTER|\bIC VA$|PRESTAMO INTRAGRUP|DEVOLUCION PRESTAMO (?:IC )?VALTERM|TRANSF\. IC|"
        r"FINANCIACION IC|PRESTAMO IC")),
)


def _fold(s: str | None) -> str:
    """Upper ASCII, accents stripped, ' | ' N43 separators and runs of spaces collapsed to one space."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return " ".join(s.replace("|", " ").split())


def views(line: Line) -> list[str]:
    """Per-field folded views (so `$` anchors work) plus the joined view when the loader built one."""
    out = [_fold(x) for x in (line.text, line.rinfo, line.ref1, line.ref2) if x]
    if line.t_join:
        out.append(line.t_join)
    return out


def classify(line: Line) -> str | None:
    vs = views(line)
    for cat, rx in _ORDER:
        for v in vs:
            if rx.search(v):
                return cat
    return None
