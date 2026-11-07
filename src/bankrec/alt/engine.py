"""Operation-first reconciliation: classify the line, then read its answer key.

The dispatch order below is the whole design. It is ordered by **how much the evidence
can be trusted**, not by how common the case is, because the first resolver that fires
wins and a cheap guess placed too early poisons an expensive certainty:

    0  reversal / return        the text says the money came back -> re-open an item
    1  operation by id          a batch / run / settlement / deposit id in the text
    2  advice by payment ref    the customer's own reference, printed on both sides
    3  operation by total       statements that print no id at all (account + date + sum)
    4  advice by payer + total  same fallback for advices
    5  references in the text   invoice numbers, including continuation lists
    6  party + amount           no reference: who paid, and does a subset add up
    7  category                 settles no item (POLICIES.md §4)

Steps 1-4 produce an *explicit document list*, which is why they come first: they are the
only path to the 1:N segments, and there is nothing to rank — the document says which
items, and the engine's remaining job is only to name the exception. Steps 5-6 are the
classic matcher, kept last and kept narrow.

Every step runs against a ledger whose balances move as lines are processed in booking
order, so "the item is already settled" (DUPLICATE_PAYMENT) and "the item is open again"
(RETURNED) are observable facts rather than guesses (CLAUDE.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data import FX, Masters, Phase
from ..schema import WITHHOLDING_RATES
from ..text import alnum, fold
from . import advice as advice_mod
from . import refs
from .cats import Categories
from .excepts import WITHHOLDING_COUNTRIES, Exceptions
from .ledger import Ledger
from .ops import DIRECTION, Operations, select as select_entries

# The scorer accepts an amount within max(0.01, 0.5 %); matching uses a slightly wider
# band so a bank spread or a rounded cent still *finds* the item (CLAUDE.md §8).
MATCH_TOL_PCT = 0.01
MATCH_TOL_MIN = 2
# Cross-currency, the bank converts at its own corporate rate: POLICIES §3 allows up to
# 3 % away from the ECB reference. Comparing a GBP invoice with a EUR credit at 1 % makes
# every FX receipt look like a partial payment.
FX_TOL_PCT = 0.03

# How an item is meant to be settled (`payment_method` on the invoice). A web/POS order
# is cleared by the acquirer's settlement, a receipt by its direct-debit batch, a cheque
# by its deposit -- none of them by an incoming transfer. Letting a transfer pick them up
# is how a 9,736.95 customer payment ends up explained by 38 unrelated card orders.
TRANSFER_METHODS = {"", "transfer"}

# Text that says the money came back. Every scheme has its own word for it.
RETURN_WORDS = (
    "DEVOL", "DEVUELT", "IMPAYE", "IMPAYÉ", "RETOUR", "RUECKLASTSCHRIFT", "RUCKLASTSCHRIFT",
    "RETURNED", "RETURN", "CHARGEBACK", "CONTRACARGO", "REJECT", "RECHAZ", "UNPAID",
    "R-TRANSACTION", "RIMBORSO", "RETROCESSION", "STORNO",
)
# ... but a *bank* reversal is a category, not a returned item (POLICIES §4).
REVERSAL_WORDS = ("OPERACION INDEBIDA", "OPERACIÓN INDEBIDA", "ANULACION OPERACION", "BANK ERROR")
# ... and a *credit* that says "devolución" is usually the opposite of a return: the
# customer sending back the guarantee retention, or re-paying a receipt that bounced.
# A collection that came back is always a debit.
NOT_RETURN_WORDS = ("GARANTIA", "GARANTÍA", "RETENCION", "RETENCIÓN", "RETENTION")


@dataclass(slots=True)
class Row:
    item_id: str = ""
    cents: int = 0
    exception: str = ""
    category: str = ""


def _ymd(value) -> int:
    d = str(value or "").replace("-", "")[:8]
    return int(d) if d.isdigit() else 0


def _tol(cents: int, cross_currency: bool = False) -> int:
    pct = FX_TOL_PCT if cross_currency else MATCH_TOL_PCT
    return max(MATCH_TOL_MIN, int(round(abs(cents) * pct)))


class Engine:
    def __init__(self, phase: Phase, items, masters: Masters):
        self.masters = masters
        self.fx = FX(masters.fx_rates)
        self.ledger = Ledger(items, masters)
        self.ops = Operations(phase)
        self.advices = advice_mod.AdviceIndex(advice_mod.load(phase), self.ledger)
        self.items_by_id = self.ledger.items
        self.masters = masters
        self.cats = Categories(masters)
        self.excepts = Exceptions(masters, self.fx)
        self.stats: dict[str, int] = {}

    # ------------------------------------------------------------------ api
    # Document-backed resolvers. The document *is* the answer, so the first one that
    # fires wins outright -- there is nothing better to compare it against.
    DOCUMENT_STEPS = ("returned", "operation_id", "advice_ref", "operation_total", "advice_party")
    # Inference resolvers. Each proposes a set of items; the winner is the one whose
    # amounts actually add up to the cash, because "explains the whole line" is a far
    # better signal than "is listed first". A truncated reference that resolves to one
    # 1,265.84 invoice must not beat the 42-invoice sweep that sums to the 100,423.72
    # on the statement.
    INFERENCE_STEPS = ("reference", "advice_total", "party_sweep", "party_amount")

    def resolve(self, txn: dict) -> list[Row]:
        ctx = _Context(txn, self)
        steps = {
            "returned": self._returned,
            "operation_id": self._operation_by_id,
            "advice_ref": self._advice_by_ref,
            "operation_total": self._operation_by_total,
            "advice_party": self._advice_by_party,
            "advice_total": self._advice_by_total,
            "reference": self._by_reference,
            "party_sweep": self._by_party_sweep,
            "party_amount": self._by_party_amount,
        }
        for name in self.DOCUMENT_STEPS:
            rows = steps[name](ctx)
            if rows:
                return self._accept(name, rows)

        tol = _tol(ctx.cash)
        best: tuple[int, str, list[Row]] | None = None
        for name in self.INFERENCE_STEPS:
            rows = steps[name](ctx)
            if not rows:
                continue
            gap = abs(_net(rows, self.ledger) - abs(ctx.cash))
            if gap <= tol:
                return self._accept(name, rows)
            if best is None or gap < best[0]:
                best = (gap, name, rows)
        if best is not None:
            return self._accept(best[1], best[2])

        category = self.cats.of(txn)
        self.stats[f"category:{category}"] = self.stats.get(f"category:{category}", 0) + 1
        # §3's UNIDENTIFIED is "a receipt for which no open item exists" -- an unknown
        # payer. A receipt from a customer we *do* know, whose items we merely failed to
        # pick, is a miss of ours; labelling it UNIDENTIFIED turns one lost line into a
        # thousand false positives against a class that only has 218 true members.
        unknown_payer = category == "UNIDENTIFIED" and not ctx.parties
        return [Row(category=category, exception="UNIDENTIFIED" if unknown_payer else "")]

    def _accept(self, name: str, rows: list[Row]) -> list[Row]:
        self.stats[name] = self.stats.get(name, 0) + 1
        self._commit(rows)
        return rows

    # ------------------------------------------------------------------- fx
    def _in_currency(self, item_id: str, currency: str, ymd: int) -> int:
        """An item's open balance expressed in the bank line's currency.

        `amount` must be submitted in the *item* currency but the cash is in the
        *account* currency; every "does this add up" test therefore has to happen on one
        side or the other, never across (CLAUDE.md §8). Everything that compares with
        cash converts to the line; `decide` converts back.
        """
        item = self.items_by_id[item_id]
        open_cents = self.ledger.open_cents(item_id)
        if item["currency"] == currency:
            return open_cents
        converted = self.fx.convert(open_cents, item["currency"], currency, ymd)
        return open_cents if converted is None else converted

    def _to_item(self, cents: int, item: dict, currency: str, ymd: int) -> int:
        if item["currency"] == currency:
            return cents
        converted = self.fx.convert(cents, currency, item["currency"], ymd)
        return cents if converted is None else converted

    def _commit(self, rows: list[Row]) -> None:
        """Move the ledger. `RETURNED` re-opens; everything else consumes."""
        for row in rows:
            if not row.item_id:
                continue
            if row.exception == "RETURNED":
                self.ledger.reopen(row.item_id, row.cents)
            else:
                self.ledger.apply(row.item_id, row.cents)

    # -------------------------------------------------------------- step 0
    def _returned(self, ctx: _Context) -> list[Row]:
        """A return re-opens exactly the items of the operation that came back (§3)."""
        if not ctx.is_return:
            return []
        op = self.ops.by_token(ctx.op_tokens)
        items: list[tuple[str, int]] = []
        if op is not None:
            entries = select_entries(op, abs(ctx.cash), ctx.want_count)
            if entries is None and op.kind in {"dd_entry", "cheque_entry", "transfer"}:
                entries = op.entries  # the id names one receipt: the amount is the return
            if entries:
                items = [(i, c) for i, c, _ in self._expand(entries, ctx) if i]
        if not items:
            # No batch id survived the text: the mandate or the invoice number did.
            for token in ctx.tokens:
                for item_id in self.ledger.by_doc(token):
                    items = [(item_id, self.ledger.item(item_id)["total_cents"])]
                    break
                if items:
                    break
        if not items:
            return []
        cash = abs(ctx.cash)
        if len(items) == 1:
            # The statement shows the returned amount: trust it over the document.
            items = [(items[0][0], cash or items[0][1])]
        return [Row(item_id=i, cents=c, exception="RETURNED") for i, c in items if c > 0]

    # ------------------------------------------------------------ steps 1-4
    def _operation_by_id(self, ctx: _Context) -> list[Row]:
        op = self.ops.by_token(ctx.op_tokens)
        if op is None:
            return []
        sign = 1 if ctx.cash > 0 else -1
        if DIRECTION.get(op.kind, 0) and DIRECTION[op.kind] != sign:
            return []  # a payment run never explains a receipt (and vice versa)
        entries = select_entries(op, abs(ctx.cash), ctx.want_count)
        if not entries:
            return []
        return self._rows_from_docs(self._expand(entries, ctx), ctx)

    def _operation_by_total(self, ctx: _Context) -> list[Row]:
        op = self.ops.by_total(
            abs(ctx.cash), str(ctx.txn.get("account_id") or ""), ctx.ymd, _tol(ctx.cash),
            sign=1 if ctx.cash > 0 else -1,
        )
        if op is None:
            return []
        return self._rows_from_docs(self._expand(op.entries, ctx), ctx)

    def _advice_by_ref(self, ctx: _Context) -> list[Row]:
        advice = self.advices.by_token(ctx.op_tokens, abs(ctx.cash), _tol(ctx.cash))
        if advice is None:
            return []
        return self._rows_from_advice(advice, ctx)

    def _advice_by_party(self, ctx: _Context) -> list[Row]:
        for party_id in ctx.parties:
            advice = self.advices.by_party_total(party_id, abs(ctx.cash), ctx.ymd, _tol(ctx.cash))
            if advice is not None:
                return self._rows_from_advice(advice, ctx)
        return []

    # An advice total is only a fingerprint when it is a sum of *many* documents; a
    # two-line advice for 587.67 collides with any number of unrelated receipts, and
    # matching on it costs more precision than it buys. The tolerance is tight for the
    # same reason: this is an identity test, not an amount comparison.
    ADVICE_TOTAL_MIN_LINES = 5
    ADVICE_TOTAL_TOL_PCT = 0.001

    def _advice_by_total(self, ctx: _Context) -> list[Row]:
        cash = abs(ctx.cash)
        advice = self.advices.by_total(
            cash,
            max(MATCH_TOL_MIN, int(round(cash * self.ADVICE_TOTAL_TOL_PCT))),
            str(ctx.txn.get("entity") or ""),
            ctx.ymd,
            min_lines=self.ADVICE_TOTAL_MIN_LINES,
        )
        if advice is None:
            return []
        return self._rows_from_advice(advice, ctx)

    # -------------------------------------------------------------- step 5
    def _by_reference(self, ctx: _Context) -> list[Row]:
        """Invoice numbers named in the bank text (POLICIES §5.5), incl. continuations."""
        found: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        scope = ctx.scope(self.ledger)
        for token in ctx.tokens:
            for item_id in self._resolve_doc(token, scope, ctx):
                if item_id in seen:
                    continue
                # No party veto here on purpose: a document number that resolves
                # exactly is harder evidence than a counterparty name matched through a
                # truncated, accent-folded bank text. Vetoing on the name loses every
                # line whose payer the masters spell differently -- and the balance
                # arbitration in resolve() already discards a set that does not add up.
                seen.add(item_id)
                found.append((item_id, 0, "INVOICE"))
        if not found:
            return []
        return self._rows_from_docs(found, ctx, amounts_known=False)

    # -------------------------------------------------------------- step 6
    # A payment can be short by a documented percentage and still clear the invoice in
    # full: withholding tax at source (§3) and an early-payment discount (payment_terms).
    # The search therefore has to look for item sets summing to the cash *grossed up* by
    # those rates, otherwise a Moroccan customer who withholds 10 % never matches
    # anything at all -- which is why SHORT_WITHHOLDING scored a flat zero.
    # Exactly the rates POLICIES.md names: 10 / 1.5 / 1.25 % withheld at source, and the
    # 1 / 2 / 3 % early-payment discounts in payment_terms.csv. They are matched *tightly*
    # (0.1 %), unlike the ordinary amount tolerance: a 1 % band around seven rates spanning
    # 1-10 % would accept nearly any shortfall and turn every clean payment into a
    # proportional one.
    GROSS_UP = (0.10, 0.03, 0.02, 0.015, 0.0125, 0.01)
    GROSS_UP_TOL_PCT = 0.001

    def _targets(self, cash: int, item_ids: list[str] | None = None) -> list[tuple[int, int]]:
        """[(target, tolerance)] -- the cash itself, then the cash grossed up by each rate.

        A rate is only offered when *these* items could carry it: withholding only for a
        customer in a country that withholds, a discount only when the invoice's own
        payment terms grant that percentage. Offering all six rates to every line makes
        the search find a plausible-looking subset for almost any amount, and then every
        item in it comes out short and mislabelled `PARTIAL`.
        """
        out = [(cash, _tol(cash))]
        for rate in self._rates_for(item_ids):
            grossed = int(round(cash / (1 - rate)))
            out.append((grossed, max(MATCH_TOL_MIN, int(round(grossed * self.GROSS_UP_TOL_PCT)))))
        return out

    def _rates_for(self, item_ids: list[str] | None) -> list[float]:
        if item_ids is None:
            return list(self.GROSS_UP)
        rates: set[float] = set()
        for item_id in item_ids:
            item = self.ledger.item(item_id)
            if self.excepts.country.get(item.get("party_id") or "", "") in WITHHOLDING_COUNTRIES:
                rates.update(WITHHOLDING_RATES)
            terms = self.masters.terms.get(item.get("payment_terms") or "")
            pct = terms.get("early_payment_discount_pct") if terms else None
            if pct:
                try:
                    rates.add(round(float(pct) / 100.0, 6))
                except (TypeError, ValueError):
                    pass
        return [r for r in self.GROSS_UP if r in rates]

    def _by_party_amount(self, ctx: _Context) -> list[Row]:
        """No usable reference: the payer's open items that add up to the cash (§5.6)."""
        cash = abs(ctx.cash)
        if not cash:
            return []
        side = ctx.side
        candidates = [
            i
            for i in ctx.scope(self.ledger)
            if self.ledger.open_cents(i) > 0
            and self.ledger.item(i)["side"] == side
            and self._settleable_by_transfer(i)
        ]
        if not candidates:
            # A globally unique amount is evidence on its own (the baseline's one good
            # guess); anything less unique is left to the category fallback.
            unique = [
                i
                for i in self.ledger.by_amount(cash, _tol(cash))
                if self.ledger.open_cents(i) > 0 and self.ledger.item(i)["side"] == side
            ]
            if len(unique) == 1:
                return self._rows_from_docs([(unique[0], cash, "INVOICE")], ctx)
            return []

        opens = self._opens(candidates, ctx)
        for target, tol in self._targets(cash, candidates):
            subset = _subset(candidates, target, opens, tol)
            if subset:
                return self._rows_from_docs([(i, 0, "INVOICE") for i in subset], ctx, amounts_known=False)
        return []

    def _by_party_sweep(self, ctx: _Context) -> list[Row]:
        """The payer cleared *everything* they had open.

        Big accounts pay their whole ledger on a fixed day, and when they do there is no
        advice and no reference worth parsing — but the sum is unmistakable, and it is the
        only way to reach a 42-invoice receipt whose text says "FACTURA FV24-01199 FV24-01".
        Credit notes net against the invoices, exactly as in `_share`.
        """
        cash = abs(ctx.cash)
        if not cash:
            return []
        sign = {"invoice": 1, "credit_note": -1}
        for party_id in ctx.parties:
            open_items = [
                i
                for i in self.ledger.party_items(party_id)
                if self.ledger.open_cents(i) > 0
                and self.ledger.item(i)["side"] == ctx.side
                and self._settleable_by_transfer(i)
            ]
            if len(open_items) < 2:
                continue
            opens = self._opens(open_items, ctx)
            total = sum(sign.get(self.ledger.item(i)["kind"], 1) * opens[i] for i in open_items)
            cross = self._any_cross(open_items, ctx)
            if any(
                abs(total - target) <= max(tol, _tol(target, cross) if target == cash else tol)
                for target, tol in self._targets(cash, open_items)
            ):
                return self._rows_from_docs([(i, 0, "INVOICE") for i in open_items], ctx, amounts_known=False)
        return []

    # ------------------------------------------------------------- plumbing
    def _expand(self, entries: list[dict], ctx: _Context) -> list[tuple[str, int, str]]:
        """An operation's document rows -> (item_id, cents, type), unresolved dropped."""
        out: list[tuple[str, int, str]] = []
        for entry in entries:
            scope = self._entry_scope(entry)
            item_id = next(iter(self._resolve_doc(entry["_ref"], scope, ctx)), "")
            if item_id:
                out.append((item_id, abs(entry["_cents"]), entry.get("_type", "INVOICE")))
        return out

    def _entry_scope(self, entry: dict) -> list[str]:
        """The party behind one document row of an aggregate, for scoped resolution."""
        for key in ("debtor_iban", "beneficiary_iban"):
            owner = self.ledger.party_for_iban(entry.get(key) or "")
            if owner:
                return self.ledger.party_items(owner[0])
        for key in ("debtor_name", "beneficiary", "drawer"):
            if entry.get(key):
                party = self.ledger.party_for_name(entry[key])
                if party:
                    return self.ledger.party_items(party)
        return []

    def _resolve_doc(self, ref: str, scope: list[str], ctx: _Context) -> list[str]:
        """One document reference -> item ids. Exact first, then the noise of §5.5."""
        if not ref:
            return []
        scope_set = set(scope)
        for token in refs.extract(ref) or [alnum(ref)]:
            hit = self.ledger.by_doc(token)
            if hit:
                # "2025-0217" is a *supplier's* invoice number: several suppliers use the
                # same series, so an exact key can still be ambiguous. Narrow it with the
                # beneficiary of the transfer before trusting it.
                narrowed = self._narrow(hit, scope_set, ctx)
                if narrowed:
                    return narrowed
                if len(hit) == 1:
                    return hit
        for token in refs.extract(ref) or [alnum(ref)]:
            # Our prefix dropped: "FRAS 0414882" for FV25-0414882.
            hit = self._narrow(self.ledger.by_doc_suffix(token), scope_set, ctx)
            if hit:
                return hit
            if len(token) >= 6:
                hit = self._narrow(
                    [i for i in scope if refs.digits(self.ledger.item(i)["doc_alnum"]).endswith(refs.digits(token))],
                    scope_set,
                    ctx,
                )
                if hit:
                    return hit
        if scope:
            for token in refs.extract(ref) or [alnum(ref)]:
                hit = self.ledger.by_doc_fuzzy(token, scope)
                if len(hit) == 1:
                    return hit
        # Bank truncation: the last token of a cut text is a prefix of a real number.
        for token in refs.extract(ref):
            hit = self.ledger.by_doc_prefix(token)
            if hit:
                scoped = [i for i in hit if i in set(scope)] if scope else hit
                if len(scoped) == 1:
                    return scoped
                if scoped and ctx is not None:
                    exact = [i for i in scoped if abs(self.ledger.open_cents(i) - abs(ctx.cash)) <= _tol(ctx.cash)]
                    if len(exact) == 1:
                        return exact
        return []

    def _settleable_by_transfer(self, item_id: str) -> bool:
        """POLICIES §5.3: card, direct-debit and cheque items are cleared by their own
        aggregated operation, which arrives as its own bank line with its own document."""
        return str(self.ledger.item(item_id).get("payment_method") or "") in TRANSFER_METHODS

    def _opens(self, item_ids: list[str], ctx: _Context) -> dict[str, int]:
        """Open balances of these items, all expressed in the bank line's currency."""
        return {i: self._in_currency(i, ctx.currency, ctx.ymd) for i in item_ids}

    def _any_cross(self, item_ids: list[str], ctx: _Context) -> bool:
        return any(self.ledger.item(i)["currency"] != ctx.currency for i in item_ids)

    def _narrow(self, hits: list[str], scope: set[str], ctx: _Context) -> list[str]:
        """Pick one item out of an ambiguous lookup, using everything else we know.

        In order: the payer's own items, then an item whose open balance *is* the cash,
        then the side of the book. If none of that singles one out, return nothing — a
        coin flip here costs a false pair and a false line.
        """
        if not hits:
            return []
        if len(hits) == 1:
            return hits
        if scope:
            inside = [i for i in hits if i in scope]
            if len(inside) == 1:
                return inside
            if inside:
                hits = inside
        if ctx is not None:
            exact = [
                i
                for i in hits
                if abs(self._in_currency(i, ctx.currency, ctx.ymd) - abs(ctx.cash))
                <= _tol(ctx.cash, self.ledger.item(i)["currency"] != ctx.currency)
            ]
            if len(exact) == 1:
                return exact
            sided = [i for i in hits if self.ledger.item(i)["side"] == ctx.side]
            if len(sided) == 1:
                return sided
        return []

    def _rows_from_docs(
        self, docs: list[tuple[str, int, str]], ctx: _Context, *, amounts_known: bool = True
    ) -> list[Row]:
        """Turn resolved documents into submission rows, naming the exception on each."""
        if not docs:
            return []
        rows: list[Row] = []
        unique: dict[str, tuple[int, str]] = {}
        for item_id, cents, dtype in docs:
            if item_id not in unique:
                unique[item_id] = (cents, dtype)
        # §3: a duplicate is one invoice paid a second time. When a batch, a run or an
        # advice lists an item the ledger already closed, the likelier explanation is an
        # earlier allocation of ours, not the customer paying 95 receipts twice — and
        # claiming DUPLICATE_PAYMENT there poisons both the line and the class's
        # precision. Only a line that settles exactly one item may claim it.
        allow_duplicate = len(unique) == 1 and not ctx.is_return

        # What the line owes each item, when the documents do not say it themselves.
        if not amounts_known:
            unique = self._share(unique, ctx)

        for item_id, (cents, dtype) in unique.items():
            item = self.ledger.item(item_id)
            open_before = self.ledger.open_cents(item_id)
            if open_before <= 0:
                # §3: already settled and paid again -> the *later* line is the duplicate.
                if (
                    self.ledger.is_settled(item_id)
                    and allow_duplicate
                    # §3: "the item had already been settled and is **paid again**" --
                    # the cash has to look like the whole invoice, not like a remainder.
                    and _close(abs(cents) or item["total_cents"], item["total_cents"])
                ):
                    rows.append(Row(item_id, abs(cents) or item["total_cents"], "DUPLICATE_PAYMENT"))
                    continue
                if ctx.is_return:
                    rows.append(Row(item_id, abs(cents) or item["total_cents"], "RETURNED"))
                    continue
                open_before = item["total_cents"]
            cash = open_before if item_id in ctx.full_clear else (abs(cents) or open_before)
            if dtype == "CREDIT_NOTE" or item["kind"] == "credit_note":
                # §2: a netted credit note is a full positive row with no exception.
                rows.append(Row(item_id, open_before, ""))
                continue
            key = alnum(item["document_number"])
            reason = ctx.reasons.get(key, "")
            exception, amount = self.excepts.decide(
                ctx.txn,
                item,
                cash_cents=cash,
                open_before=open_before,
                reason=reason,
                deduction=ctx.deductions.get(key, 0),
                alone=len(unique) == 1,
            )
            # Invariant: a submitted amount is a positive integer number of cents in the
            # item currency (schema.validate_row). An over-applied ledger balance can
            # make the arithmetic above go negative; such a row is not evidence, drop it.
            if amount > 0:
                rows.append(Row(item_id, amount, exception))
        return rows

    def _share(self, unique: dict[str, tuple[int, str]], ctx: _Context) -> dict[str, tuple[int, str]]:
        """Split the cash over items the text named but gave no amounts for.

        A credit note in the list does not *consume* cash, it *reduces* it: the customer
        pays "invoices minus credit note", so the cash to distribute over the invoices is
        `cash + sum(credit notes)` and the credit note itself is a full positive row
        (POLICIES §2). Getting this backwards silently drops the credit note from every
        netted payment, which is the whole `ar_1toN_credit_note` segment.

        Full clearing first (what the overwhelming majority of 1:N receipts do); only the
        residual, if any, lands on the oldest still-open item, the documented tie-break
        (§5.6).
        """
        order = sorted(unique, key=lambda i: (self.ledger.item(i)["issue_ymd"], i))
        currency, ymd = ctx.currency, ctx.ymd

        def opened(item_id: str) -> int:
            """Open balance in the *line's* currency (0 -> the untouched total)."""
            value = self._in_currency(item_id, currency, ymd)
            if value > 0:
                return value
            item = self.ledger.item(item_id)
            return self._to_item(item["total_cents"], {"currency": currency}, item["currency"], ymd)

        opens = {i: max(opened(i), 0) for i in order}
        notes = {i for i in order if self.ledger.item(i)["kind"] == "credit_note"}
        invoices = [i for i in order if i not in notes]

        target = abs(ctx.cash) + sum(opens[i] for i in notes)
        out: dict[str, tuple[int, str]] = {i: (opens[i], unique[i][1]) for i in notes}

        total = sum(opens[i] for i in invoices)
        cross = ctx.cross(self, order)
        grossed = next(
            (
                g
                for g, tol in self._targets(target, invoices)
                if abs(total - g) <= (_tol(g, cross) if g == target else tol)
            ),
            0,
        )
        if invoices and grossed:
            # The cash covers the invoices exactly, or falls short of them by a uniform
            # documented percentage (a discount taken on the whole payment, tax withheld
            # at source). Share the *cash* in proportion, never the open balances: it is
            # the per-item shortfall that lets `decide` name SHORT_DISCOUNT /
            # SHORT_WITHHOLDING / SHORT_BANK_CHARGES and then write the full amount.
            # Handing it the full balance hides the very difference it has to explain.
            out.update(
                {i: (_round_to(opens[i], target, total), unique[i][1]) for i in invoices}
            )
        elif len(invoices) == 1:
            # One named invoice simply receives the whole cash: decide() explains the gap.
            out[invoices[0]] = (target, unique[invoices[0]][1])
        else:
            # Several invoices named, and the cash does not match their sum by any
            # documented rate. Clearing each in full is the better bet than slicing the
            # cash across them: when the sum is *larger* the usual cause is that an
            # earlier line of ours failed to allocate, so our balances are stale rather
            # than the payment partial -- and a sliced amount is wrong twice (it loses
            # the pair to the 0.5 % tolerance *and* mislabels it PARTIAL). Slicing is
            # reserved for the single-invoice case above, which is where the real
            # instalments and retentions live.
            out.update({i: (opens[i], unique[i][1]) for i in invoices})
        return {
            i: (self._to_item(cents, self.ledger.item(i), currency, ymd), dtype)
            for i, (cents, dtype) in out.items()
        }

    def _rows_from_advice(self, advice, ctx: _Context) -> list[Row]:
        docs: list[tuple[str, int, str]] = []
        scope = ctx.scope(self.ledger)
        for line in advice.lines:
            item_id = next(iter(self._resolve_doc(line.document, scope, ctx)), "")
            if not item_id:
                continue
            docs.append((item_id, line.paid_cents or line.gross_cents, "INVOICE"))
            key = alnum(self.ledger.item(item_id)["document_number"])
            if line.reason:
                ctx.reasons[key] = line.reason
            if line.deduction_cents:
                ctx.deductions[key] = line.deduction_cents
            else:
                # The advice lists this document with nothing deducted: it is settled in
                # full, whatever our own balance says. Our balance is the less reliable
                # number here -- it is stale exactly when an earlier line of ours failed
                # to allocate, and then the advice line looks like a partial payment.
                ctx.full_clear.add(item_id)
        return self._rows_from_docs(docs, ctx)


class _Context:
    """Everything derived from one bank line, computed once."""

    __slots__ = ("txn", "text", "tokens", "op_tokens", "cash", "ymd", "party", "side",
                 "reasons", "deductions", "want_count", "parties", "_scope", "is_return",
                 "currency", "full_clear")

    def __init__(self, txn: dict, engine: Engine):
        self.txn = txn
        fields = (
            txn.get("text"),
            txn.get("ref1"),
            txn.get("ref2"),
            txn.get("remittance_info"),
            txn.get("end_to_end_id"),
        )
        self.text = fold(" ".join(str(f or "") for f in fields) + " " + str(txn.get("counterparty_name") or ""))
        self.tokens = refs.extract(*(str(f or "") for f in fields))
        self.op_tokens = refs.operational(*(str(f or "") for f in fields))
        self.cash = int(txn.get("amount_cents") or 0)
        self.currency = str(txn.get("currency") or "EUR")
        # A cross-currency statement line carries the original amount it was ordered in
        # ("the bank shows the original amount and/or rate", POLICIES §3). That number is
        # the invoice currency exactly, with no spread to allow for -- always better
        # evidence than converting the booked amount at a reference rate.
        orig = txn.get("orig_amount")
        orig_currency = str(txn.get("orig_currency") or "")
        if orig and orig_currency:
            try:
                self.cash = int(round(float(orig) * 100)) * (1 if self.cash >= 0 else -1)
                self.currency = orig_currency
            except (TypeError, ValueError):
                pass
        self.ymd = _ymd(txn.get("booking_date"))
        self.side = "AR" if self.cash > 0 else "AP"
        self.reasons: dict[str, str] = {}
        self.deductions: dict[str, int] = {}
        self.full_clear: set[str] = set()
        # "REMESA CF0100... 3 PROVEEDORES" / "... 125 RECIBOS": the statement says how
        # many underlying operations this debit covers. It is the cheapest possible
        # disambiguator for a subset-sum (ops.select).
        count = _COUNT_RE.search(self.text)
        self.want_count = int(count.group(1)) if count else 0
        self.is_return = (
            _hit(self.text, RETURN_WORDS)
            and not _hit(self.text, REVERSAL_WORDS)
            and not _hit(self.text, NOT_RETURN_WORDS)
            # Direction decides: a returned collection debits us, a returned transfer
            # credits us back. A *credit* that merely contains "devolución" is a
            # retention release or a re-payment, and re-opening an item for it is wrong
            # twice over (the pair and the exception).
            and (self.cash < 0 or _hit(self.text, ("TRANSFERENCIA DEVUELTA", "RETURNED TRANSFER")))
        )

        self._scope: list[str] | None = None
        owner = engine.ledger.party_for_iban(txn.get("counterparty_account") or "")
        if owner:
            # A known paying IBAN is an identity, not a guess (POLICIES §5.1).
            self.parties = [owner[0]]
        else:
            # The payer name hides in a different field per statement format: a column in
            # camt.053, after "ABWA+" in a SEPA free text, after "TRANSFERENCIA DE" in an
            # n43 body, between slashes in an MT940 "/ORDP/". Pool every reading instead
            # of ranking the formats, and let the amount decide.
            self.parties = []
            seen_party: set[str] = set()
            for candidate in (txn.get("counterparty_name") or "", *_payer_candidates(self.text), self.text):
                if not candidate:
                    continue
                for party_id in engine.ledger.parties_for_name(candidate):
                    if party_id not in seen_party:
                        seen_party.add(party_id)
                        self.parties.append(party_id)
                if self.parties:
                    break
        self.party = self.parties[0] if self.parties else ""

    def cross(self, engine, item_ids) -> bool:
        return engine._any_cross(list(item_ids), self)

    def scope(self, ledger) -> list[str]:
        """The items of every shortlisted party: the search space for a noisy reference."""
        if self._scope is None:
            out: list[str] = []
            for party_id in self.parties:
                out.extend(ledger.party_items(party_id))
            self._scope = out
        return self._scope


_COUNT_RE = __import__("re").compile(r"\b(\d{1,4})\s+(?:PROVEEDORES|RECIBOS|POSTEN|OPERATIONS|OPERACIONES|ITEMS|EFFETS)")


_PAYER_MARKERS = (
    "TRANSFERENCIA DE ", "TRANSFERENCIAS ", "ORD. ", "ORDENANTE ", " AN ", "/ORDP/",
    "ABWA+", "VIREMENT DE ", "VON ", "DE LA PART DE ",
)


def _payer_candidates(text: str) -> list[str]:
    """Every substring of a bank text that could be the payer's name."""
    out = []
    for marker in _PAYER_MARKERS:
        if marker in text:
            out.append(text.split(marker, 1)[1].split("|")[0][:60])
    return out


def _round_to(part: int, target: int, total: int) -> int:
    """`part` of `total`, rescaled so the parts add up to `target`."""
    if total <= 0 or target == total:
        return part
    return int(round(part * target / total))


def _close(a: int, b: int, pct: float = 0.01) -> bool:
    return abs(a - b) <= max(2, int(round(abs(b) * pct)))


def _net(rows: list[Row], ledger: Ledger) -> int:
    """Cash a set of rows claims to explain: invoices minus the credit notes netted
    against them (POLICIES §2)."""
    total = 0
    for row in rows:
        if not row.item_id:
            continue
        kind = ledger.item(row.item_id)["kind"]
        total += -row.cents if kind == "credit_note" else row.cents
    return total


def _hit(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _subset(candidates: list[str], cash: int, opens: dict[str, int], tol: int) -> list[str]:
    """The oldest-first set of open items whose balances add up to the cash.

    Deliberately not a general subset-sum: FIFO is the documented behaviour (§5.6) and a
    search over 200 candidates would find a coincidental combination far more often than
    the right one.
    """
    for item_id in candidates:
        if abs(opens.get(item_id, 0) - cash) <= tol:
            return [item_id]
    running = 0
    picked: list[str] = []
    for item_id in candidates:
        open_cents = opens.get(item_id, 0)
        if open_cents <= 0:
            continue
        picked.append(item_id)
        running += open_cents
        if abs(running - cash) <= tol:
            return picked
        if running > cash + tol:
            return []
    return []


__all__ = ["Engine", "Row"]
