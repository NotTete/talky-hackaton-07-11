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
from ..text import alnum, fold
from . import advice as advice_mod
from . import refs
from .cats import Categories
from .excepts import Exceptions
from .ledger import Ledger
from .ops import DIRECTION, Operations, select as select_entries

# The scorer accepts an amount within max(0.01, 0.5 %); matching uses a slightly wider
# band so a bank spread or a rounded cent still *finds* the item (CLAUDE.md §8).
MATCH_TOL_PCT = 0.01
MATCH_TOL_MIN = 2

# Text that says the money came back. Every scheme has its own word for it.
RETURN_WORDS = (
    "DEVOLUCION", "DEVOLUCIÓN", "DEVUELTO", "IMPAYE", "IMPAYÉ", "RETOUR", "RUECKLASTSCHRIFT",
    "RUCKLASTSCHRIFT", "RETURNED", "RETURN", "CHARGEBACK", "CONTRACARGO", "REJECT", "RECHAZO",
    "UNPAID", "R-TRANSACTION", "RIMBORSO", "RETROCESSION", "STORNO",
)
# ... but a *bank* reversal is a category, not a returned item (POLICIES §4).
REVERSAL_WORDS = ("OPERACION INDEBIDA", "OPERACIÓN INDEBIDA", "ANULACION OPERACION", "BANK ERROR")


@dataclass(slots=True)
class Row:
    item_id: str = ""
    cents: int = 0
    exception: str = ""
    category: str = ""


def _ymd(value) -> int:
    d = str(value or "").replace("-", "")[:8]
    return int(d) if d.isdigit() else 0


def _tol(cents: int) -> int:
    return max(MATCH_TOL_MIN, int(round(abs(cents) * MATCH_TOL_PCT)))


class Engine:
    def __init__(self, phase: Phase, items, masters: Masters):
        self.masters = masters
        self.fx = FX(masters.fx_rates)
        self.ledger = Ledger(items, masters)
        self.ops = Operations(phase)
        self.advices = advice_mod.AdviceIndex(advice_mod.load(phase), self.ledger)
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
    INFERENCE_STEPS = ("reference", "party_sweep", "party_amount")

    def resolve(self, txn: dict) -> list[Row]:
        ctx = _Context(txn, self)
        steps = {
            "returned": self._returned,
            "operation_id": self._operation_by_id,
            "advice_ref": self._advice_by_ref,
            "operation_total": self._operation_by_total,
            "advice_party": self._advice_by_party,
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
        return [Row(category=category, exception="UNIDENTIFIED" if category == "UNIDENTIFIED" else "")]

    def _accept(self, name: str, rows: list[Row]) -> list[Row]:
        self.stats[name] = self.stats.get(name, 0) + 1
        self._commit(rows)
        return rows

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
        if not _hit(ctx.text, RETURN_WORDS) or _hit(ctx.text, REVERSAL_WORDS):
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
        advice = self.advices.by_token(ctx.op_tokens)
        if advice is None:
            return []
        return self._rows_from_advice(advice, ctx)

    def _advice_by_party(self, ctx: _Context) -> list[Row]:
        for party_id in ctx.parties:
            advice = self.advices.by_party_total(party_id, abs(ctx.cash), ctx.ymd, _tol(ctx.cash))
            if advice is not None:
                return self._rows_from_advice(advice, ctx)
        return []

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
                item = self.ledger.item(item_id)
                if ctx.parties and item["party_id"] not in ctx.parties and len(ctx.tokens) > 1:
                    continue
                seen.add(item_id)
                found.append((item_id, 0, "INVOICE"))
        if not found:
            return []
        return self._rows_from_docs(found, ctx, amounts_known=False)

    # -------------------------------------------------------------- step 6
    def _by_party_amount(self, ctx: _Context) -> list[Row]:
        """No usable reference: the payer's open items that add up to the cash (§5.6)."""
        cash = abs(ctx.cash)
        if not cash:
            return []
        side = ctx.side
        candidates = [
            i
            for i in ctx.scope(self.ledger)
            if self.ledger.open_cents(i) > 0 and self.ledger.item(i)["side"] == side
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

        subset = _subset(candidates, cash, self.ledger, _tol(cash))
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
                if self.ledger.open_cents(i) > 0 and self.ledger.item(i)["side"] == ctx.side
            ]
            if len(open_items) < 2:
                continue
            total = sum(sign.get(self.ledger.item(i)["kind"], 1) * self.ledger.open_cents(i) for i in open_items)
            if abs(total - cash) <= _tol(cash):
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
        for token in refs.extract(ref) or [alnum(ref)]:
            hit = self.ledger.by_doc(token)
            if hit:
                return hit
        scope_set = set(scope)
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
            exact = [i for i in hits if abs(self.ledger.open_cents(i) - abs(ctx.cash)) <= _tol(ctx.cash)]
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

        # What the line owes each item, when the documents do not say it themselves.
        if not amounts_known:
            unique = self._share(unique, ctx)

        for item_id, (cents, dtype) in unique.items():
            item = self.ledger.item(item_id)
            open_before = self.ledger.open_cents(item_id)
            if open_before <= 0:
                # §3: already settled and paid again -> the *later* line is the duplicate.
                if self.ledger.is_settled(item_id):
                    rows.append(Row(item_id, abs(cents) or item["total_cents"], "DUPLICATE_PAYMENT"))
                    continue
                open_before = item["total_cents"]
            cash = abs(cents) or open_before
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
        opens = {i: (self.ledger.open_cents(i) or self.ledger.item(i)["total_cents"]) for i in order}
        notes = [i for i in order if self.ledger.item(i)["kind"] == "credit_note"]
        invoices = [i for i in order if i not in set(notes)]

        target = abs(ctx.cash) + sum(max(opens[i], 0) for i in notes)
        out: dict[str, tuple[int, str]] = {i: (max(opens[i], 0), unique[i][1]) for i in notes}

        total = sum(max(opens[i], 0) for i in invoices)
        if invoices and abs(total - target) <= _tol(target):
            out.update({i: (opens[i], unique[i][1]) for i in invoices})
            return out
        if len(invoices) == 1:
            # One named invoice simply receives the whole cash: decide() explains the gap.
            out[invoices[0]] = (target, unique[invoices[0]][1])
            return out
        left = target
        for item_id in invoices:
            take = min(max(opens[item_id], 0), left)
            if take <= 0:
                continue
            out[item_id] = (take, unique[item_id][1])
            left -= take
            if left <= 0:
                break
        return out

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
        return self._rows_from_docs(docs, ctx)


class _Context:
    """Everything derived from one bank line, computed once."""

    __slots__ = ("txn", "text", "tokens", "op_tokens", "cash", "ymd", "party", "side",
                 "reasons", "deductions", "want_count", "parties", "_scope")

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
        self.ymd = _ymd(txn.get("booking_date"))
        self.side = "AR" if self.cash > 0 else "AP"
        self.reasons: dict[str, str] = {}
        self.deductions: dict[str, int] = {}
        # "REMESA CF0100... 3 PROVEEDORES" / "... 125 RECIBOS": the statement says how
        # many underlying operations this debit covers. It is the cheapest possible
        # disambiguator for a subset-sum (ops.select).
        count = _COUNT_RE.search(self.text)
        self.want_count = int(count.group(1)) if count else 0

        self._scope: list[str] | None = None
        owner = engine.ledger.party_for_iban(txn.get("counterparty_account") or "")
        if owner:
            # A known paying IBAN is an identity, not a guess (POLICIES §5.1).
            self.parties = [owner[0]]
        else:
            self.parties = engine.ledger.parties_for_name(txn.get("counterparty_name") or "")
            if not self.parties:
                self.parties = engine.ledger.parties_for_name(_payer_from_text(self.text))
            if not self.parties:
                # n43 puts the payer anywhere in the body ("92844359-AGUAS BLUE S.A.");
                # the whole text is a worse name but a better net, and every consumer of
                # a party here is gated on the amount adding up.
                self.parties = engine.ledger.parties_for_name(self.text)
        self.party = self.parties[0] if self.parties else ""

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


def _payer_from_text(text: str) -> str:
    """n43 lines put the payer in the body: 'TRANSFERENCIA DE <name>', 'ORD. <name>'."""
    for marker in _PAYER_MARKERS:
        if marker in text:
            tail = text.split(marker, 1)[1]
            return tail.split("|")[0][:60]
    return ""


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


def _subset(candidates: list[str], cash: int, ledger: Ledger, tol: int) -> list[str]:
    """The oldest-first set of open items whose balances add up to the cash.

    Deliberately not a general subset-sum: FIFO is the documented behaviour (§5.6) and a
    search over 200 candidates would find a coincidental combination far more often than
    the right one.
    """
    for item_id in candidates:
        if abs(ledger.open_cents(item_id) - cash) <= tol:
            return [item_id]
    running = 0
    picked: list[str] = []
    for item_id in candidates:
        open_cents = ledger.open_cents(item_id)
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
