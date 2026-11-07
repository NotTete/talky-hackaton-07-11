"""Allocation: pick the subset of candidate items one bank line settles.

The cash amount is the constraint; candidates are ranked by evidence. We look for

* a single item matching the cash (full payment, FX spread, discount, withholding);
* the documents of an aggregated operation (batch / run), when the sums agree;
* a small subset sum over the top candidates (one transfer paying N invoices).

`POLICIES.md` then decides the exception per row (`rules.exception_for`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .candidates import Candidate
from .config import Settings
from .data import Masters
from .items import ItemBook, ymd
from .rules import exception_for


@dataclass(slots=True)
class AllocationRow:
    pos: int
    item_id: str
    cents: int  # amount cleared by this bank line, in the item currency
    exception: str = ""


@dataclass(slots=True)
class Allocation:
    rows: list[AllocationRow] = field(default_factory=list)
    residual_cents: int = 0
    evidence: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.rows)


class Matcher:
    def __init__(self, book: ItemBook, masters: Masters, settings: Settings):
        self.book = book
        self.masters = masters
        self.settings = settings

    # ------------------------------------------------------------------ api
    def allocate(self, txn: dict, candidates: list[Candidate]) -> Allocation:
        if not candidates:
            return Allocation()
        book = self.book
        booking = ymd(str(txn.get("booking_date") or ""))
        target = abs(int(txn["amount_cents"]))
        txn_currency = str(txn.get("currency") or "EUR")
        tol = self.settings.scoring.tolerant(target)

        open_cands = [c for c in candidates if c.is_open]
        closed_cands = [c for c in candidates if not c.is_open]

        # value of each candidate in the *transaction* currency, for comparing
        # against the cash amount
        def comp(c: Candidate) -> int:
            item = book.get(c.pos)
            cents = c.applied_cents if c.applied_cents is not None else item.open_cents
            if item.currency == txn_currency:
                return cents
            converted = book.fx.convert(cents, item.currency, txn_currency, booking)
            return converted if converted is not None else cents

        # ------------------------------------------------ 0. remittance advice
        advice = self._advice_set(txn, open_cands, comp, target, tol)
        if advice is not None:
            return advice

        # ---------------------------------------------------- 1. single item
        single = self._single(txn, open_cands, comp, target, tol, booking)
        if single is not None:
            return single

        # ------------------------------------- 1b. partial payment of one item
        partial = self._partial(txn, open_cands, comp, target, tol, booking)
        if partial is not None:
            return partial

        # ------------------------------------------- 2. aggregated documents
        batch = self._aggregated(txn, open_cands, comp, target, tol, booking)
        if batch is not None:
            return batch

        # ------------------------------------------------------ 3. subset sum
        subset = self._subset(txn, open_cands, comp, target, tol, booking)
        if subset is not None:
            return subset

        # ------------------------------- 4. closed items: duplicate / returned
        closed = self._closed(txn, closed_cands, target, booking)
        if closed is not None:
            return closed

        return Allocation()

    # -------------------------------------------------------------- helpers
    def _row(self, txn: dict, pos: int, cents: int, hint: str = "") -> AllocationRow:
        item = self.book.get(pos)
        exc = hint or exception_for(
            txn=txn,
            item=item,
            applied_cents=cents,
            remaining_cents=max(item.open_cents - cents, 0),
            masters=self.masters,
            settings=self.settings,
        )
        return AllocationRow(pos=pos, item_id=item.item_id, cents=cents, exception=exc)

    def _single(self, txn, cands, comp, target, tol, booking) -> Allocation | None:
        book = self.book
        txn_currency = str(txn.get("currency") or "EUR")
        best: tuple[float, Candidate] | None = None
        for cand in cands[:20]:
            item = book.get(cand.pos)
            value = comp(cand)
            if not value:
                continue
            delta = abs(value - target)
            fx = item.currency != txn_currency
            # FX spread / withholding / charges can explain up to 3% before we
            # even look at the policies.
            limit = max(tol, int(round(target * self.settings.fx.tolerance_pct)))
            if delta <= limit or (delta <= tol and not fx):
                rank = delta / max(target, 1) - 0.5 * cand.score
                if best is None or rank < best[0]:
                    best = (rank, cand)
        if best is None:
            return None
        cand = best[1]
        item = book.get(cand.pos)
        # full clearing: the amount is the item's open amount in the item currency,
        # unless an advice/batch file states the exact applied amount.
        cents = cand.applied_cents if cand.applied_cents is not None else item.open_cents
        row = self._row(txn, cand.pos, cents, cand.hint)
        self._apply([row])
        return Allocation([row], residual_cents=0, evidence="single:" + cand.reason)

    def _advice_set(self, txn, cands, comp, target, tol) -> Allocation | None:
        """A remittance advice lists the documents: if they add up, they are the answer."""
        advised = [c for c in cands if c.reason == "advice"]
        if not advised:
            return None
        limit = max(tol, int(round(target * self.settings.fx.tolerance_pct)))
        total = sum(comp(c) for c in advised)
        # Some advice documents may already be settled (or missing from the item
        # tables). As long as most of the cash is explained by the documents we can
        # resolve, emit them: recall is what an advice is for.
        if total > target + limit or total < target * 0.5:
            return None
        rows = []
        for cand in advised:
            item = self.book.get(cand.pos)
            cents = cand.applied_cents if cand.applied_cents is not None else item.open_cents
            rows.append(self._row(txn, cand.pos, cents, cand.hint))
        self._apply(rows)
        return Allocation(rows, residual_cents=target - total, evidence="advice")

    def _partial(self, txn, cands, comp, target, tol, booking) -> Allocation | None:
        """A strongly referenced item paid short: PARTIAL / SHORT_DISPUTE."""
        book = self.book
        txn_currency = str(txn.get("currency") or "EUR")
        for cand in cands[:10]:
            if cand.applied_cents is not None:
                continue
            if not (cand.reason.startswith("ref") or cand.reason.startswith("advice")):
                continue
            item = book.get(cand.pos)
            open_value = comp(cand)
            if target <= tol or open_value <= tol:
                continue
            if target >= open_value:
                continue  # overpayment, handled elsewhere
            # do not turn a rounding/charges difference into a PARTIAL
            if open_value - target <= max(tol, int(round(open_value * 0.02))):
                continue
            if target < open_value * 0.05:
                continue  # implausibly small: likely a fee, not a partial payment
            cents = book.fx.convert(target, txn_currency, item.currency, booking) or target
            cents = min(cents, item.open_cents)
            row = self._row(txn, cand.pos, cents, cand.hint)
            if row.exception == "":
                row.exception = "PARTIAL"
            book.settle(cand.pos, cents)
            return Allocation([row], residual_cents=0, evidence="partial")

    def _aggregated(self, txn, cands, comp, target, tol, booking) -> Allocation | None:
        """Trust a document group when the documents it names sum to the cash."""
        grouped = [c for c in cands if c.reason in {"dd_batch", "card", "cheque", "payment_run"}]
        if not grouped:
            return None
        book = self.book
        limit = max(tol, int(round(target * self.settings.fx.tolerance_pct)))
        total = sum(comp(c) for c in grouped)
        if abs(total - target) > limit:
            # keep only the highest-scoring group (all entries carry the same reason)
            return None
        rows = []
        for cand in grouped:
            item = book.get(cand.pos)
            cents = cand.applied_cents if cand.applied_cents is not None else item.open_cents
            rows.append(self._row(txn, cand.pos, cents, cand.hint))
        self._apply(rows)
        return Allocation(rows, residual_cents=target - total, evidence="group")

    def _subset(self, txn, cands, comp, target, tol, booking) -> Allocation | None:
        if not cands:
            return None
        m = self.settings.matching
        # Deduplicate by value, keep the best-scored candidate for each amount.
        pool: list[Candidate] = []
        seen_amounts: dict[int, int] = {}
        for cand in cands:
            value = comp(cand)
            if value <= 0:
                continue
            if value in seen_amounts:
                continue
            seen_amounts[value] = len(pool)
            pool.append(cand)
            if len(pool) >= m.max_candidates:
                break
        if len(pool) < 2:
            return None

        values = [comp(c) for c in pool]
        budget = [m.subset_search_nodes]
        # advice/batch documents can list many items in one line
        max_items = max(m.max_items_per_txn, len(pool)) if any(c.reason != "amount" for c in pool) else m.max_items_per_txn
        solution = _search(values, target, tol, max_items, budget)
        if solution is None:
            return None
        total = sum(values[i] for i in solution)
        rows = []
        for i in solution:
            cand = pool[i]
            item = self.book.get(cand.pos)
            # multi-item lines clear their items in full unless a document says otherwise
            cents = cand.applied_cents if cand.applied_cents is not None else item.open_cents
            rows.append(self._row(txn, cand.pos, cents, cand.hint))
        self._apply(rows)
        return Allocation(rows, residual_cents=target - total, evidence="subset")

    def _closed(self, txn, closed_cands, target, booking) -> Allocation | None:
        """A reference to an already-settled item: duplicate payment or return."""
        if not closed_cands:
            return None
        book = self.book
        txn_currency = str(txn.get("currency") or "EUR")
        cand = closed_cands[0]
        item = book.get(cand.pos)
        cents = book.fx.convert(target, txn_currency, item.currency, booking) or target
        hint = cand.hint or exception_for(
            txn=txn,
            item=item,
            applied_cents=cents,
            remaining_cents=item.open_cents,
            masters=self.masters,
            settings=self.settings,
        )
        if hint not in {"DUPLICATE_PAYMENT", "RETURNED"}:
            return None
        row = AllocationRow(pos=cand.pos, item_id=item.item_id, cents=cents, exception=hint)
        if hint == "RETURNED":
            book.reopen(cand.pos, cents)
        return Allocation([row], residual_cents=0, evidence="closed:" + hint)

    def _apply(self, rows: list[AllocationRow]) -> None:
        for row in rows:
            if row.exception == "RETURNED":
                continue
            remaining = self.book.settle(row.pos, row.cents)
            # PARTIAL: the row amount is the cash applied, not the open amount
            if row.exception == "PARTIAL" and remaining > 0:
                row.cents = max(row.cents, 0)
            if remaining == 0 and row.exception == "PARTIAL":
                # the item was actually cleared: drop the PARTIAL flag
                row.exception = ""


def _search(values: list[int], target: int, tol: int, max_items: int, budget: list[int]) -> list[int] | None:
    """Bounded depth-first subset sum. Values are cents, target is the cash amount."""
    order = sorted(range(len(values)), key=lambda i: -values[i])
    ranked = [values[i] for i in order]
    prefix = [0]
    for v in ranked:
        prefix.append(prefix[-1] + v)
    chosen: list[int] = []
    best: list[int] | None = None

    def dfs(start: int, remaining: int, depth: int) -> bool:
        nonlocal best
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        if chosen and abs(remaining) <= tol:
            best = chosen.copy()
            return True
        if depth >= max_items or start >= len(ranked):
            return False
        # even taking every remaining value cannot reach the target
        if remaining > prefix[-1] - prefix[start] + tol:
            return False
        for i in range(start, len(ranked)):
            v = ranked[i]
            if v > remaining + tol:
                continue
            chosen.append(i)
            if dfs(i + 1, remaining - v, depth + 1):
                return True
            chosen.pop()
            if budget[0] <= 0:
                break
        return False

    dfs(0, target, 0)
    return [order[i] for i in best] if best else None


__all__ = ["Allocation", "AllocationRow", "Matcher"]
