"""Deciding what a bank line settles — the part you have to build.

The baseline below is deliberately small: it does the two things that already work
well (an exact document number in the text, and the right party + the right amount)
and leaves every harder case marked with TODO. Each step is one of the documented
patterns from POLICIES.md §5, so you can grow it branch by branch and re-score on
TRAIN after each one.

Score impact of the pieces you will add (from the reference scorer):
  allocation F1      50 %   -> 1:N payments, batches, credit notes netted
  transaction accuracy 30 % -> the exact set of (item, amount, exception) per line
  exception macro-F1 20 %   -> PARTIAL / SHORT_* / RETURNED / DUPLICATE / FX
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass

import polars as pl

from .data import FX, Masters
from .rules import exception_for
from .schema import CREDIT_NOTE_PREFIXES, amount_tolerance_cents
from .text import alnum, name_similarity, name_tokens, reference_tokens, ref_variants


@dataclass(slots=True)
class Allocation:
    """One row of the submission: how much of `item_id` this bank line clears."""

    item_id: str
    cents: int
    exception: str = ""


class ItemIndex:
    """Lookup structures over the open items. Build once, query per bank line."""

    def __init__(self, items: pl.DataFrame, masters: Masters):
        # item_id -> row dict. Polars keeps the bulk work fast; the matcher only ever
        # touches a handful of these rows per bank line.
        self.items: dict[str, dict] = {row["item_id"]: row for row in items.iter_rows(named=True)}
        self.masters = masters
        self.fx = FX(masters.fx_rates)
        self.open: dict[str, int] = {i: int(r["open_cents"]) for i, r in self.items.items()}

        # document number (and its leading-zero / digit-core variants) -> item_ids
        self.by_ref: dict[str, list[str]] = defaultdict(list)
        for item_id, row in self.items.items():
            for variant in ref_variants(str(row["document_number"])):
                self.by_ref[variant].append(item_id)

        # party -> item_ids, and a token index to find the party from a truncated name
        self.by_party: dict[str, list[str]] = defaultdict(list)
        for item_id, row in self.items.items():
            self.by_party[row["party_id"]].append(item_id)

        self.party_by_token: dict[str, set[str]] = defaultdict(set)
        self.party_names: dict[str, str] = dict(masters.party_names)
        for df, id_col in ((masters.customers, "customer_id"), (masters.suppliers, "supplier_id")):
            if df.is_empty() or "name" not in df.columns:
                continue
            for row in df.select(pl.col(id_col), pl.col("name")).iter_rows(named=True):
                self.party_names[row[id_col]] = row["name"] or ""
                for tok in name_tokens(row["name"]):
                    if len(tok) >= 4:
                        self.party_by_token[tok].add(row[id_col])

        # exact open amount -> item_ids, plus the sorted list of live amounts.
        # Kept in sync by `settle()`, so amount lookups stay O(log n) instead of a scan.
        self._by_amount: dict[int, set[str]] = defaultdict(set)
        for item_id, cents in self.open.items():
            if cents:
                self._by_amount[cents].add(item_id)
        self._amounts: list[int] = sorted(self._by_amount)

    # ------------------------------------------------------------------ api
    def open_items(self, ids) -> list[str]:
        return [i for i in ids if self.open.get(i, 0) > 0]

    def settle(self, item_id: str, cents: int) -> int:
        """Apply cash to an item; returns what is still open."""
        old = self.open.get(item_id, 0)
        new = max(old - max(cents, 0), 0)
        self.open[item_id] = new
        self._reindex_amount(item_id, old, new)
        return new

    def _reindex_amount(self, item_id: str, old: int, new: int) -> None:
        if old == new:
            return
        if old:
            bucket = self._by_amount.get(old)
            if bucket is not None:
                bucket.discard(item_id)
                if not bucket:
                    del self._by_amount[old]
                    index = bisect.bisect_left(self._amounts, old)
                    if index < len(self._amounts) and self._amounts[index] == old:
                        self._amounts.pop(index)
        if new:
            if new not in self._by_amount:
                bisect.insort(self._amounts, new)
            self._by_amount[new].add(item_id)

    def items_by_amount(self, cents: int, tol: int) -> list[str]:
        """Open items whose open amount is within +-tol cents (bisect, O(log n + hits))."""
        lo = bisect.bisect_left(self._amounts, cents - tol)
        hi = bisect.bisect_right(self._amounts, cents + tol)
        # sorted: set iteration is hash-seed dependent, and reproducibility matters
        return sorted(item_id for amount in self._amounts[lo:hi] for item_id in self._by_amount[amount])

    def in_item_currency(self, cents: int, item_currency: str, line_currency: str, ymd: int) -> int:
        """The cash amount expressed in the item currency (for FX lines)."""
        if item_currency == line_currency:
            return cents
        return self.fx.convert(cents, line_currency, item_currency, ymd) or cents


class Matcher:
    def __init__(self, index: ItemIndex):
        self.index = index
        self.masters = index.masters

    # -------------------------------------------------------------- matching
    def match(self, txn: dict) -> list[Allocation]:
        """Return the allocations of one bank line (empty = it settles no item)."""
        cash = abs(int(txn["amount_cents"]))
        tol = amount_tolerance_cents(cash)
        ymd = int(str(txn.get("booking_date") or "0").replace("-", "")[:8] or 0)
        line_currency = str(txn.get("currency") or "EUR")
        text = " ".join(str(txn.get(k) or "") for k in ("text", "ref1", "ref2", "remittance_info"))

        # 1. Exact document references in the text (the highest-precision signal).
        ids: list[str] = []
        for token in sorted(reference_tokens(text)):
            for variant in sorted(ref_variants(token)):
                ids.extend(self.index.by_ref.get(variant, ()))
        referenced = list(dict.fromkeys(self.index.open_items(ids)))
        if referenced:
            rows = self._rows(txn, referenced)
            if self._sum(rows, line_currency, ymd) <= cash + tol:
                return rows
            # the text names items that do not add up (1:N advice, deductions...)
            # TODO: subset-sum / remittance advice / credit note netted (POLICIES §5.3-5.4)

        # 2. The right party and the right amount (1:1 transfer).
        for party_id in self._parties(txn):
            for item_id in self.index.by_party.get(party_id, ()):
                amount = self.index.open.get(item_id, 0)
                if not amount:
                    continue
                expected = self.index.in_item_currency(cash, self.index.items[item_id]["currency"], line_currency, ymd)
                if abs(amount - expected) <= tol:
                    return self._rows(txn, [item_id])

        # 3. Unique amount across all open items (no reference, no name).
        #    TODO: restrict by date window / entity, add FIFO for the monthly fees
        #    ("same amount, several open invoices" -> oldest first, POLICIES §5.6).
        same_amount = self.index.items_by_amount(cash, tol)
        if len(same_amount) == 1:
            return self._rows(txn, same_amount)

        return []

    # -------------------------------------------------------------- helpers
    def _parties(self, txn: dict) -> list[str]:
        """Party ids implied by the line: IBAN first, then a fuzzy name match."""
        out: list[str] = []
        owner = self.masters.iban_owner.get(alnum(str(txn.get("counterparty_account") or "")))
        if owner:
            out.append(owner[0])

        text = f"{txn.get('counterparty_name') or ''} {txn.get('text') or ''}"
        tokens = name_tokens(text)
        votes: dict[str, int] = defaultdict(int)
        for tok in tokens:
            for party_id in self.index.party_by_token.get(tok, ()):
                votes[party_id] += 1
        for party_id, hits in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0])):
            name = self._party_name(party_id)
            if hits >= 2 or name_similarity(tokens, name_tokens(name)) >= 0.5:
                out.append(party_id)
                if len(out) >= 5:
                    break
        return out

    def _party_name(self, party_id: str) -> str:
        return self.index.party_names.get(party_id, "")

    def _rows(self, txn: dict, item_ids: list[str]) -> list[Allocation]:
        rows: list[Allocation] = []
        for item_id in item_ids:
            item = self.index.items[item_id]
            cents = self.index.open.get(item_id, 0)
            if not cents:
                continue
            rows.append(
                Allocation(
                    item_id=item_id,
                    cents=cents,
                    exception=exception_for(
                        txn=txn,
                        item=item,
                        applied_cents=cents,
                        remaining_cents=0,
                        masters=self.masters,
                    ),
                )
            )
        for row in rows:
            self.index.settle(row.item_id, row.cents)
        return rows

    def _sum(self, rows: list[Allocation], line_currency: str, ymd: int) -> int:
        total = 0
        for row in rows:
            item = self.index.items[row.item_id]
            total += self.index.in_item_currency(row.cents, item["currency"], line_currency, ymd)
        return total


__all__ = ["Allocation", "ItemIndex", "Matcher", "CREDIT_NOTE_PREFIXES"]
