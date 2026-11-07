"""Aggregated banking operations: the answer keys that live on disk.

Four of the hardest segments are not matching problems at all — the exact item list is
written down in `direct_debit_batches/`, `card_settlements/`, `cheque_deposits/` and
`payment_runs/`, and the only question is which file row belongs to which bank line.
This module builds one index keyed by **every identifier that can show up in bank text**
(batch id, settlement id, deposit id, run id, end-to-end id, mandate, cheque number,
merchant id) plus a (account, date, total) fallback for the banks that print none of them.

An `Operation` is deliberately dumb: a kind, the keys it answers to, a total, and the
document rows it contains. Turning its `document_number`s into item ids is `engine`'s job,
because that is where the ledger's state lives (CLAUDE.md §5).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field

from ..data import Phase, to_cents
from ..text import alnum

# Dates slip between the document and the statement (value date, weekend, maturity).
DATE_WINDOW = 12


def _ymd(value: str) -> int:
    d = str(value or "").replace("-", "")[:8]
    return int(d) if d.isdigit() else 0


@dataclass(slots=True)
class Operation:
    kind: str                       # dd_batch | dd_entry | card | cheque | run | transfer
    account_id: str
    ymd: int
    total_cents: int                # what the bank line should show (net of acquirer fee)
    entries: list[dict] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    def docs(self) -> list[tuple[str, int, str]]:
        """(document reference, amount in cents, document type) for every entry."""
        return [(e["_ref"], e["_cents"], e.get("_type", "INVOICE")) for e in self.entries]

    def groups(self) -> list[tuple[int, list[dict]]]:
        """(amount, entries) per *beneficiary transfer* inside this operation.

        A confirming remittance is one file but several bank debits -- the bank pays each
        maturity slice separately, and the text says only how many suppliers are in this
        one ("REMESA CF0100... 3 PROVEEDORES"). The indivisible unit is the transfer, so
        selecting which slice a line paid is a subset-sum over these groups, never over
        individual invoices.
        """
        buckets: dict[str, list[dict]] = {}
        for entry in self.entries:
            key = str(entry.get("end_to_end_id") or entry.get("beneficiary_iban") or id(entry))
            buckets.setdefault(key, []).append(entry)
        out = []
        for entries in buckets.values():
            transfer = entries[0].get("_transfer")
            amount = abs(transfer) if transfer else sum(abs(e["_cents"]) for e in entries)
            out.append((amount, entries))
        return out


class Operations:
    """Build once per phase; query per bank line."""

    def __init__(self, phase: Phase):
        self.by_key: dict[str, Operation] = {}
        self._by_total: dict[int, list[Operation]] = defaultdict(list)
        self.all: list[Operation] = []

        self._load_direct_debits(phase)
        self._load_cards(phase)
        self._load_cheques(phase)
        self._load_payment_runs(phase)
        self._totals = sorted(self._by_total)

    # ------------------------------------------------------------- building
    def _add(self, op: Operation) -> None:
        self.all.append(op)
        if op.total_cents:
            self._by_total[op.total_cents].append(op)
        for key in op.keys:
            token = alnum(key)
            # First writer wins: ids are unique per kind, and a collision means a
            # degenerate key (an empty string, a shared mandate) we should not trust.
            if token and len(token) >= 6:
                self.by_key.setdefault(token, op)

    def _load_direct_debits(self, phase: Phase) -> None:
        df = phase.documents("direct_debit_batches")
        if df.is_empty():
            return
        df = df.with_columns(to_cents("amount").alias("_cents"))
        has_total = "batch_total" in df.columns
        if has_total:
            df = df.with_columns(to_cents("batch_total").alias("_total"))
        for (batch_id,), rows in df.group_by("batch_id", maintain_order=True):
            entries = list(rows.iter_rows(named=True))
            for e in entries:
                e["_ref"] = e.get("remittance_info") or ""
            total = entries[0]["_total"] if has_total else sum(e["_cents"] for e in entries)
            keys = [str(batch_id)]
            op = Operation("dd_batch", str(entries[0].get("account_id") or ""),
                           _ymd(entries[0].get("collection_date")), total, entries, keys)
            self._add(op)
            # Each mandate/e2e also identifies *one* receipt: a return names only that one.
            for e in entries:
                single = Operation("dd_entry", op.account_id, op.ymd, e["_cents"], [e],
                                   [k for k in (e.get("end_to_end_id"), e.get("mandate_id")) if k])
                self._add(single)

    def _load_cards(self, phase: Phase) -> None:
        df = phase.documents("card_settlements")
        if df.is_empty():
            return
        df = df.with_columns(to_cents("gross").alias("_cents"), to_cents("net").alias("_net"))
        for (settlement_id,), rows in df.group_by("settlement_id", maintain_order=True):
            entries = list(rows.iter_rows(named=True))
            for e in entries:
                e["_ref"] = e.get("order_number") or ""
            # POLICIES §4: the orders clear at *gross*, the bank credits the *net*.
            self._add(Operation("card", str(entries[0].get("account_id") or ""),
                                _ymd(entries[0].get("settlement_date")),
                                sum(e["_net"] for e in entries), entries, [str(settlement_id)]))

    def _load_cheques(self, phase: Phase) -> None:
        df = phase.documents("cheque_deposits")
        if df.is_empty():
            return
        df = df.with_columns(to_cents("amount").alias("_cents"))
        has_total = "deposit_total" in df.columns
        if has_total:
            df = df.with_columns(to_cents("deposit_total").alias("_total"))
        for (deposit_id,), rows in df.group_by("deposit_id", maintain_order=True):
            entries = list(rows.iter_rows(named=True))
            for e in entries:
                e["_ref"] = e.get("stub_reference") or ""
            total = entries[0]["_total"] if has_total else sum(e["_cents"] for e in entries)
            op = Operation("cheque", str(entries[0].get("account_id") or ""),
                           _ymd(entries[0].get("deposit_date")), total, entries, [str(deposit_id)])
            self._add(op)
            for e in entries:
                self._add(Operation("cheque_entry", op.account_id, op.ymd, e["_cents"], [e],
                                    [str(e.get("cheque_number") or "")]))

    def _load_payment_runs(self, phase: Phase) -> None:
        df = phase.documents("payment_runs")
        if df.is_empty():
            return
        df = df.with_columns(
            to_cents("transfer_amount").alias("_transfer"),
            to_cents("document_amount").alias("_cents"),
        )
        for (run_id,), run_rows in df.group_by("run_id", maintain_order=True):
            run_entries = list(run_rows.iter_rows(named=True))
            for e in run_entries:
                e["_ref"] = e.get("document_number") or ""
                e["_type"] = e.get("document_type") or "INVOICE"

            # One debit per beneficiary transfer: end_to_end_id is on the bank line.
            by_e2e: dict[str, list[dict]] = defaultdict(list)
            for e in run_entries:
                by_e2e[str(e.get("end_to_end_id") or "")].append(e)
            for e2e, entries in by_e2e.items():
                if not e2e:
                    continue
                self._add(Operation("transfer", str(entries[0].get("debtor_account_id") or ""),
                                    _ymd(entries[0].get("execution_date")),
                                    abs(entries[0]["_transfer"]), entries, [e2e]))

            # ... and one debit for the whole run (aggregated / confirming remittance).
            # transfer_amount repeats per document row, so sum it once per transfer.
            total = sum(abs(rows[0]["_transfer"]) for rows in by_e2e.values())
            self._add(Operation("run", str(run_entries[0].get("debtor_account_id") or ""),
                                _ymd(run_entries[0].get("execution_date")),
                                total, run_entries, [str(run_id)]))

    # -------------------------------------------------------------- lookups
    def by_token(self, tokens: list[str]) -> Operation | None:
        """The operation a bank line names outright. Longest key first: a bank line
        carries both `PR010025061275-0083` and `PR010025061275`, and the transfer is the
        more specific answer."""
        best: Operation | None = None
        best_len = 0
        for token in tokens:
            op = self.by_key.get(token)
            if op is not None and len(token) > best_len:
                best, best_len = op, len(token)
        return best

    def by_total(self, cents: int, account_id: str, ymd: int, tol: int) -> Operation | None:
        """Fallback for statements that print no operation id at all."""
        lo = bisect.bisect_left(self._totals, cents - tol)
        hi = bisect.bisect_right(self._totals, cents + tol)
        best, best_key = None, None
        for total in self._totals[lo:hi]:
            for op in self._by_total[total]:
                if op.kind in {"dd_entry", "cheque_entry"}:
                    continue
                if op.account_id and account_id and op.account_id != account_id:
                    continue
                if op.ymd and ymd and abs(op.ymd - ymd) > DATE_WINDOW:
                    continue
                key = (abs(op.total_cents - cents), op.kind, op.keys[0] if op.keys else "")
                if best_key is None or key < best_key:
                    best, best_key = op, key
        return best





def select(op: Operation, target: int, want_count: int = 0) -> list[dict] | None:
    """Which entries of `op` explain `target` cents of cash.

    Returns `None` when the operation cannot explain the cash at all — which is the
    point of this function. A batch id appears in the text of the *collection* line and
    again in the text of its **fee** line ("COMISION REMESA 03250603661 125 RECIBOS",
    -18.75 against a 89,776.76 batch); expanding the batch for that fee line used to add
    125 phantom rows and mark every receipt as a duplicate. An operation only answers
    for a line whose amount it can actually account for.
    """
    tol = max(2, int(round(target * 0.005)))
    if abs(op.total_cents - target) <= tol:
        return op.entries

    # One entry of the batch: a single returned receipt, a single cheque.
    singles = [e for e in op.entries if abs(abs(e["_cents"]) - target) <= tol]
    if len(singles) == 1:
        return singles

    groups = op.groups()
    if len(groups) > 1:
        chosen = _subset_sum([g[0] for g in groups], target, tol, want_count)
        if chosen is not None:
            return [e for i in chosen for e in groups[i][1]]
    return None


# Guard rails for the subset search: a run has a few dozen transfers, not thousands, and
# an unbounded search would find a coincidental combination before the right one.
MAX_GROUPS = 64
MAX_STATES = 60_000


def _subset_sum(amounts: list[int], target: int, tol: int, want_count: int) -> list[int] | None:
    """Indices of the amounts that add up to `target`, preferring `want_count` of them.

    Exact DP over (cardinality, running sum). The cardinality is part of the key because
    the bank text states it ("3 PROVEEDORES"), and that single hint turns an ambiguous
    search into a unique answer.
    """
    if not amounts or len(amounts) > MAX_GROUPS or target <= 0:
        return None
    if sum(amounts) < target - tol:
        return None
    states: dict[tuple[int, int], list[int]] = {(0, 0): []}
    for index, amount in enumerate(amounts):
        if amount <= 0:
            continue
        for (count, total), picked in list(states.items()):
            key = (count + 1, total + amount)
            if key[1] > target + tol or key in states:
                continue
            states[key] = [*picked, index]
            if len(states) > MAX_STATES:
                return None
    hits = [(count, picked) for (count, total), picked in states.items()
            if picked and abs(total - target) <= tol]
    if not hits:
        return None
    if want_count:
        exact = [p for c, p in hits if c == want_count]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None  # ambiguous even with the hint: refuse rather than guess
    if len(hits) == 1:
        return hits[0][1]
    # Several combinations add up: only trust it when they all agree, which happens when
    # the amounts are duplicated rather than genuinely ambiguous.
    first = sorted(hits[0][1])
    return hits[0][1] if all(sorted(p) == first for _, p in hits) else None

__all__ = ["Operation", "Operations", "select"]
