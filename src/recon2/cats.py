"""Category router (DESIGN §3): category for a line that has NO item evidence.

Order: treasury regex -> text overrides (proforma, refund, interest by sign) -> learned majority
with back-off over (fmt, code, subcode, sign, tok2) -> (fmt, code, sign) -> (sign, tok2) -> (sign)
-> default (credit UNIDENTIFIED, debit BANK_FEES).  Bank codes cannot separate UNIDENTIFIED from
item receipts, so this is only consulted when every item resolver came back empty.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from recon2 import treasury
from recon2.model import Line

ITEM = "_ITEM"   # pseudo-class counted for the informational prior is_category_line()
MIN_SUPPORT = (3, 5, 5, 1)

_TOK = re.compile(r"[A-Z0-9]+")
_DIG = re.compile(r"\d")
_PROFORMA = re.compile(r"PROFORMA|\bPF\d")
_REFUND = re.compile(r"REFUND|DEVOLUCION PAGO|DUPLICAD|PAGO DOBLE|DOUBLE PAYMENT|REMBOURSEMENT|RUECKERSTATTUNG|"
                     r"ERSTATTUNG|RIMBORSO|DOPPELZAHLUNG|PAIEMENT DOUBLE|SALDO A FAVOR")
_SREFUND = re.compile(r"REEMBOLSO|ABONO PAGO|DEVOLUCION SALDO|GUTSCHRIFT|AVOIR|RESTITUZIONE|REIMBURSEMENT")
_PAYREF = re.compile(r"\bFRAS?\b|\bFAC|CONFIRMING|BULK PAYMENT|\bPR\d{8,}|\bFV[- ]?\d")
_RUNMARK = re.compile(r"CONFIRMING|REMESA CF\d|\bCF\d{10}|\bPR\d{10}")
_TREAS = {"CASH_POOL", "INTERNAL_TRANSFER", "FX_DEAL", "INTERCOMPANY"}
_INTEREST =re.compile(r"LIQUIDACION INTERESES|INTERES(?:ES)? |INTEREST|ZINSEN|INTERETS|INTERESSI")


def tok2(text: str | None) -> str:
    toks = _TOK.findall(treasury._fold(text))[:2]
    return " ".join(_DIG.sub("9", t) for t in toks)


def keys(fmt: str, code: str, subcode: str, sign: str, t2: str) -> tuple[str, str, str, str]:
    return (f"{fmt}|{code}|{subcode}|{sign}|{t2}", f"{fmt}|{code}|{sign}", f"{sign}|{t2}", sign)


def line_keys(line: Line) -> tuple[str, str, str, str]:
    sign = "C" if line.cash > 0 else "D"
    return keys(line.fmt, line.code, line.subcode, sign, tok2(line.text))


def learn(train_root: Path, until: str, out: Path) -> None:
    import polars as pl  # IO only
    train_root = Path(train_root)
    bt = pl.read_csv(train_root / "bank_transactions.csv.gz", infer_schema_length=0).filter(
        pl.col("booking_date") <= until)
    g = pl.read_csv(train_root / "golden_allocations.csv.gz", infer_schema_length=0)
    lab = g.group_by("bank_txn_id").agg(
        pl.col("category").drop_nulls().first().alias("cat"), pl.col("item_id").drop_nulls().len().alias("n"))
    df = bt.join(lab, on="bank_txn_id", how="inner").select(
        "statement_format", "bank_code", "bank_subcode", "amount", "text", "cat", "n")
    levels: list[dict[str, dict[str, int]]] = [defaultdict(lambda: defaultdict(int)) for _ in range(4)]
    for fmt, code, sub, amt, text, cat, n in df.iter_rows():
        sign = "D" if (amt or "").startswith("-") else "C"
        c = ITEM if n else (cat or "UNIDENTIFIED")
        for lv, k in zip(levels, keys(fmt or "", code or "", sub or "", sign, tok2(text))):
            lv[k][c] += 1
    art = {"until": until, "levels": [{k: dict(sorted(v.items())) for k, v in sorted(lv.items())} for lv in levels]}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(art, sort_keys=True, separators=(",", ":")))


def _majority(counts: dict[str, int]) -> str | None:
    best = None
    for c, n in counts.items():
        if c == ITEM:
            continue
        if best is None or (n, c) > (best[0], best[1]):   # deterministic tie-break by name
            best = (n, c)
    return best[1] if best else None


class Router:
    def __init__(self, artefact: Path):
        art = json.loads(Path(artefact).read_text())
        self.levels: list[dict[str, dict[str, int]]] = art["levels"]
        # precompute the majority category per key (category-only counts) once
        self._maj = [{k: (_majority(v), sum(n for c, n in v.items() if c != ITEM)) for k, v in lv.items()}
                     for lv in self.levels]

    def category(self, line: Line) -> str:
        t = treasury.classify(line)
        if t:
            return t
        credit = line.cash > 0
        v = " ".join(treasury.views(line))
        if credit and _PROFORMA.search(v):
            return "CUSTOMER_ADVANCE"
        if _REFUND.search(v) or _SREFUND.search(v):
            # no item evidence left: a refund credit is a supplier paying back (the APC case was an item)
            return "SUPPLIER_REFUND" if credit else "CUSTOMER_REFUND"
        if _INTEREST.search(v):
            return "INTEREST_INCOME" if credit else "INTEREST_EXPENSE"
        for lv, k, ms in zip(self._maj, line_keys(line), MIN_SUPPORT):
            m = lv.get(k)
            if m and m[0] and m[1] >= ms:
                if m[0] in _TREAS and _RUNMARK.search(v):   # payment-run debit: never a treasury move
                    continue
                if m[0] == "CUSTOMER_REFUND" and _PAYREF.search(v):   # payment run / invoice refs: not a refund
                    continue
                return m[0]
        return "UNIDENTIFIED" if credit else "BANK_FEES"

    def is_category_line(self, line: Line) -> float:
        for lv, k in zip(self.levels, line_keys(line)):
            c = lv.get(k)
            if c:
                tot = sum(c.values())
                if tot >= 5:
                    return 1.0 - c.get(ITEM, 0) / tot
        return 0.5
