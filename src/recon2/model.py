"""Frozen contract shared by every recon2 module.  Change only with the integrator.

Conventions (non-negotiable, see docs/recon2/DESIGN.md):
  * money is int cents, signed for bank lines, positive for items;
  * dates are day ordinals (datetime.date.toordinal()), never YYYYMMDD ints;
  * nothing iterated into the output is a set/dict without an explicit sort;
  * every module is pure: takes these records, returns these records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import os
from pathlib import Path

# Raw data lives in the main checkout (gitignored); override with RECON2_DATA.
DATA = Path(os.environ.get("RECON2_DATA", "/home/tete/Documents/Talky/talky-hackaton-07-11/data"))

KINDS = ("ARI", "ARC", "API", "APC")  # item kind == item_id prefix, always


def day(iso: str | None) -> int:
    """'2025-06-03' -> ordinal; '' / None -> 0."""
    if not iso:
        return 0
    return date(int(iso[0:4]), int(iso[5:7]), int(iso[8:10])).toordinal()


def iso(d: int) -> str:
    return date.fromordinal(d).isoformat() if d else ""


@dataclass(slots=True)
class Item:
    idx: int                 # dense id 0..N-1 (position in Book.items)
    item_id: str             # ARI-2025-0000618
    kind: str                # ARI | ARC | API | APC  (== item_id[:3])
    entity: str
    party_id: str            # customer_id / supplier_id / credit-note party
    party_name: str
    doc: str                 # our document_number (AR) / supplier_invoice_number (AP)
    issue: int               # day ordinal
    due: int
    ccy: str
    total: int               # cents, positive
    open: int                # cents, current open balance (mutable)
    terms: str = ""          # payment_terms code ('' if unknown)
    method: str = ""         # transfer | dd | card | cheque | ''
    po: str = ""             # customer_po / our_po
    related: str = ""        # credit note related_document
    carried: bool = False    # came from open_items_at_start
    residue: bool = False    # 0 < open < total and not the first payment (see DESIGN §residues)


@dataclass(slots=True)
class Line:
    idx: int                 # position in chronological order (booking_date, bank_txn_id)
    txn: str                 # bank_txn_id
    account: str
    entity: str              # ALWAYS from the line (accounts may be missing in masters)
    bank: str
    fmt: str                 # n43 | mt940 | camt053
    day: int                 # booking date ordinal
    value_day: int
    cash: int                # signed cents, account currency
    ccy: str                 # account currency
    orig: int | None         # instructed amount cents (abs) when present
    orig_ccy: str
    code: str
    subcode: str
    ref1: str
    ref2: str
    text: str
    cpty_name: str
    cpty_acct: str
    e2e: str
    rinfo: str               # camt remittance_info
    # derived by textviews.build(line) — filled by the loader
    t_join: str = ""         # folded upper, n43 38-char wraps glued (DESIGN §textviews)
    tokens: tuple[str, ...] = ()   # alnum ref-like tokens from all fields, sorted unique


@dataclass(slots=True)
class Alloc:
    item: int                # Item.idx
    amount: int              # cents, ITEM currency, > 0  (as it will be written)
    exc: str                 # '' or one code of schema.EXCEPTIONS
    rule: str                # decide() rule id, e.g. 'E7'


@dataclass(slots=True)
class Hyp:
    """A resolver proposal: which items this line settles (amounts decided later by decide())."""
    resolver: str            # e.g. 'ops.dd', 'advice', 'ref.exact', 'party.amount'
    tier: int                # 0 op/advice, 1 return/dup/rep, 2 exact ref, 3 noisy ref, 4 party+amount, 5 FIFO/month
    items: list[int]         # Item.idx, in named order
    cn: list[int] = field(default_factory=list)       # credit notes netted on this line
    is_return: bool = False
    advice: dict | None = None   # per-item advice facts: {item_idx: {'paid':c,'ded':c,'reason':str}}
    partial_item: int | None = None  # n_to_m: the single item that takes the residual
    note: str = ""


@dataclass(slots=True)
class Decision:
    """What the writer emits for one line, plus the trace for the fixing agent."""
    txn: str
    allocs: list[Alloc] = field(default_factory=list)
    category: str = ""       # set iff allocs is empty
    exc: str = ""            # only 'UNIDENTIFIED' with category UNIDENTIFIED
    trace: dict = field(default_factory=dict)  # see DESIGN §trace
