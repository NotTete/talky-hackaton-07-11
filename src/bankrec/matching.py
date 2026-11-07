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
import heapq
from collections import defaultdict
from dataclasses import dataclass

import polars as pl

from .data import FX, Masters
from .preprocess import AdviceIndex, DocumentContext
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
        self.party_tokens: dict[str, frozenset[str]] = {
            pid: frozenset(name_tokens(name)) for pid, name in self.party_names.items()
        }
        for df, id_col in ((masters.customers, "customer_id"), (masters.suppliers, "supplier_id")):
            if df.is_empty() or "name" not in df.columns:
                continue
            for row in df.select(pl.col(id_col), pl.col("name")).iter_rows(named=True):
                self.party_names[row[id_col]] = row["name"] or ""
                for tok in name_tokens(row["name"]):
                    if len(tok) >= 4:
                        self.party_by_token[tok].add(row[id_col])

        # NOTE: do not prune common tokens ("POOL", "WASSER"). They are weak evidence on
        # their own, but the vote count across several tokens is what disambiguates a
        # truncated name — pruning them costs ~4 points of score on the sample.

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

    def items_by_amount(self, cents: int, tol: int, limit: int = 250) -> list[str]:
        """Open items whose open amount is within +-tol cents.

        `tol` is 0.5 % of the cash amount, so a 1 M EUR line looks at a 5 000 EUR window.
        Hundreds of items can live there and none of them is a better guess than another:
        return [] instead of ranking noise (it is also what keeps the run fast).
        """
        lo = bisect.bisect_left(self._amounts, cents - tol)
        hi = bisect.bisect_right(self._amounts, cents + tol)
        if hi - lo > limit:
            return []
        # sorted: set iteration is hash-seed dependent, and reproducibility matters
        return sorted(item_id for amount in self._amounts[lo:hi] for item_id in self._by_amount[amount])

    def in_item_currency(self, cents: int, item_currency: str, line_currency: str, ymd: int) -> int:
        """The cash amount expressed in the item currency (for FX lines)."""
        if item_currency == line_currency:
            return cents
        return self.fx.convert(cents, line_currency, item_currency, ymd) or cents


class Matcher:
    def __init__(self, index: ItemIndex, context: DocumentContext | None = None, advices: AdviceIndex | None = None):
        self.index = index
        self.masters = index.masters
        self.context = context
        self.advices = advices

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
        #
        #    Order matters for speed *and* for accuracy. The amount window is highly
        #    selective and cheap; the name index is neither (a token like "POOL" expands
        #    to thousands of parties). So: take the items whose amount matches, then ask
        #    which of them belongs to a party named in the text.
        iban_party = self._party_from_iban(txn)
        if iban_party:
            rows = self._party_amount(txn, [iban_party], cash, tol, line_currency, ymd)
            if rows:
                return rows

        rows = self._amount_and_name(txn, cash, tol, line_currency, ymd)
        if rows:
            return rows

        # 2b. Cross-currency or sparse candidates: fall back to name blocking, which is
        #     slower but does not depend on the amount index being in the same currency.
        rows = self._party_amount(txn, self._parties_by_name(txn), cash, tol, line_currency, ymd)
        if rows:
            return rows

        # 2c. Aggregated operations: a direct-debit batch, card settlement, cheque
        #     deposit or payment run is one bank line for many documents (POLICIES
        #     §5.2) — resolve each document of the group against the same item index.
        if self.context is not None:
            group = self.context.group_for(txn, tol)
            if group is not None:
                rows = self._group_rows(txn, group)
                if rows:
                    return rows

        # 2d. Remittance advice: the only place a 1:N payment's documents and a
        #     SHORT_DISPUTE deduction reason are named (POLICIES §5.4).
        if self.advices is not None and len(self.advices):
            payer_blob = " ".join(str(txn.get(k) or "") for k in ("counterparty_name", "text", "remittance_info"))
            advice = self.advices.find(payer_blob, cash, ymd, tol)
            if advice is not None:
                rows = self._advice_rows(txn, advice)
                if rows:
                    return rows

        # 3. Unique amount across all open items (no reference, no name).
        #    TODO: restrict by date window / entity, add FIFO for the monthly fees
        #    ("same amount, several open invoices" -> oldest first, POLICIES §5.6).
        same_amount = self.index.items_by_amount(cash, tol)
        if len(same_amount) == 1:
            return self._rows(txn, same_amount)

        return []

    # -------------------------------------------------------------- helpers
    def _party_from_iban(self, txn: dict) -> str | None:
        owner = self.masters.iban_owner.get(alnum(str(txn.get("counterparty_account") or "")))
        return owner[0] if owner else None

    def _amount_and_name(self, txn, cash, tol, line_currency, ymd) -> list[Allocation]:
        """Items in the amount window whose party name matches the bank text."""
        window = self.index.items_by_amount(cash, tol)
        if not window:
            return []
        tokens = name_tokens(f"{txn.get('counterparty_name') or ''} {txn.get('text') or ''}")
        if not tokens:
            return []
        best: tuple[float, str] | None = None
        for item_id in window:
            item = self.index.items[item_id]
            expected = self.index.in_item_currency(cash, item["currency"], line_currency, ymd)
            if abs(self.index.open[item_id] - expected) > tol:
                continue
            score = name_similarity(tokens, self.index.party_tokens.get(item["party_id"], frozenset()))
            if score >= 0.5 and (best is None or score > best[0]):
                best = (score, item_id)
        return self._rows(txn, [best[1]]) if best else []

    def _party_amount(self, txn, party_ids, cash, tol, line_currency, ymd) -> list[Allocation]:
        """Best item of these parties whose open amount matches the cash."""
        for party_id in party_ids:
            for item_id in self.index.by_party.get(party_id, ()):
                amount = self.index.open.get(item_id, 0)
                if not amount:
                    continue
                expected = self.index.in_item_currency(
                    cash, self.index.items[item_id]["currency"], line_currency, ymd
                )
                if abs(amount - expected) <= tol:
                    return self._rows(txn, [item_id])
        return []

    def _parties_by_name(self, txn: dict) -> list[str]:
        """Fuzzy party match from the truncated counterparty name in the bank text.

        Classic blocking: a name has one *selective* token and a few common ones. Start
        from the smallest candidate set (the rare token) and intersect, instead of
        expanding every token into thousands of parties and counting votes. If even the
        rarest token is common, there is nothing to disambiguate: give up.
        """
        tokens = name_tokens(f"{txn.get('counterparty_name') or ''} {txn.get('text') or ''}")
        if not tokens:
            return []
        sets = [s for tok in tokens if (s := self.index.party_by_token.get(tok)) is not None]
        if not sets:
            return []
        sets.sort(key=len)
        smallest = sets[0]
        if len(smallest) > 2_000:  # "POOL" alone is not evidence
            return []

        votes: dict[str, int] = dict.fromkeys(smallest, 1)
        for other in sets[1:]:
            for party_id in list(votes):
                if party_id in other:
                    votes[party_id] += 1
        if not votes:
            return []

        out: list[str] = []
        for party_id, hits in heapq.nsmallest(5, votes.items(), key=lambda kv: (-kv[1], kv[0])):
            if hits >= 2 or name_similarity(tokens, self.index.party_tokens.get(party_id, frozenset())) >= 0.5:
                out.append(party_id)
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

    def _apply_one(self, txn: dict, item_id: str, applied_cents: int, reason: str = "") -> Allocation | None:
        """Settle a *specific* amount against one item (unlike `_rows`, not the full open
        balance) — used when the source document (batch entry / advice line) states its
        own amount, which may be less than what is still open (§3 SHORT_*/PARTIAL).
        `reason` is the advice's stated deduction reason, if any (-> SHORT_DISPUTE)."""
        item = self.index.items.get(item_id)
        open_cents = self.index.open.get(item_id, 0)
        if item is None or open_cents <= 0 or applied_cents <= 0:
            return None
        applied = min(applied_cents, open_cents)
        allocation = Allocation(
            item_id=item_id,
            cents=applied,
            exception=exception_for(
                txn=txn,
                item=item,
                applied_cents=applied,
                remaining_cents=open_cents - applied,
                masters=self.masters,
                reason=reason,
            ),
        )
        self.index.settle(item_id, applied)
        return allocation

    def _resolve_document(self, doc_number: str, amount_cents: int, iban: str) -> str | None:
        """One document inside an aggregated operation / advice -> the item it settles."""
        if doc_number:
            ids: list[str] = []
            for variant in sorted(ref_variants(doc_number)):
                ids.extend(self.index.open_items(self.index.by_ref.get(variant, ())))
            if ids:
                return sorted(dict.fromkeys(ids))[0]
        if iban:
            owner = self.masters.iban_owner.get(alnum(iban))
            if owner and amount_cents:
                tol = amount_tolerance_cents(amount_cents)
                for item_id in sorted(self.index.by_party.get(owner[0], ())):
                    if abs(self.index.open.get(item_id, 0) - amount_cents) <= tol:
                        return item_id
        return None

    # document kind -> (candidate reference fields, amount field, debtor/beneficiary IBAN field)
    _GROUP_ENTRY_FIELDS = {
        "dd_batch": (("remittance_info", "mandate_id"), "amount_cents", "debtor_iban"),
        "card": (("order_number",), "gross_cents", ""),
        "cheque": (("stub_reference",), "amount_cents", ""),
        "payment_run": (("document_number",), "transfer_cents", "beneficiary_iban"),
    }

    def _group_rows(self, txn: dict, group) -> list[Allocation]:
        """Resolve every document of an aggregated operation against the item index."""
        doc_fields, amount_field, iban_field = self._GROUP_ENTRY_FIELDS.get(group.kind, ((), "", ""))
        # two entries can resolve to the same item (e.g. an instalment split across the
        # same batch) — aggregate first so each item is only ever applied once per line
        # (CLAUDE.md §19: no duplicate (bank_txn_id, item_id) rows).
        amounts: dict[str, int] = defaultdict(int)
        for entry in group.entries:
            amount_cents = abs(int(entry.get(amount_field) or 0))
            if not amount_cents:
                continue
            doc_number = next((str(entry.get(f) or "") for f in doc_fields if entry.get(f)), "")
            iban = str(entry.get(iban_field) or "") if iban_field else ""
            item_id = self._resolve_document(doc_number, amount_cents, iban)
            if item_id is not None:
                amounts[item_id] += amount_cents
        return self._apply_many(txn, amounts)

    def _advice_rows(self, txn: dict, advice) -> list[Allocation]:
        """Resolve every line of a remittance advice against the item index."""
        amounts: dict[str, int] = defaultdict(int)
        reasons: dict[str, str] = {}
        for entry in advice.entries:
            applied = entry.paid_cents or entry.gross_cents
            if not applied:
                continue
            item_id = self._resolve_document(entry.document_number, applied, "")
            if item_id is not None:
                amounts[item_id] += applied
                if entry.reason:
                    reasons[item_id] = entry.reason
        return self._apply_many(txn, amounts, reasons)

    def _apply_many(self, txn: dict, amounts: dict[str, int], reasons: dict[str, str] | None = None) -> list[Allocation]:
        rows: list[Allocation] = []
        for item_id in sorted(amounts):
            row = self._apply_one(txn, item_id, amounts[item_id], (reasons or {}).get(item_id, ""))
            if row:
                rows.append(row)
        return rows

    def _sum(self, rows: list[Allocation], line_currency: str, ymd: int) -> int:
        total = 0
        for row in rows:
            item = self.index.items[row.item_id]
            total += self.index.in_item_currency(row.cents, item["currency"], line_currency, ymd)
        return total


__all__ = ["Allocation", "ItemIndex", "Matcher", "CREDIT_NOTE_PREFIXES"]
