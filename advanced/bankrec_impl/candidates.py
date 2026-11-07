"""Blocking: which open items could this bank line settle?

Four evidence sources, strongest first (POLICIES.md §5):

1. **Document references** in the bank text / references / remittance info.
2. **Batch documents** — direct-debit batches, card settlements, cheque deposits and
   our own payment runs tell us exactly which documents one aggregated line pays.
3. **The counterparty** — IBAN (known paying account) or a fuzzy name match against
   the customer/supplier master, combined with the amount.
4. **Amount only** — last resort, and only trusted when it is unambiguous.

Everything returns `Candidate` objects; `match.py` decides the final allocation.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
import pandas as pd

from .advices import AdviceIndex, load_advices
from .config import Settings
from .data import PhaseData, to_cents
from .items import ItemBook, ymd
from .normalize import (
    name_similarity,
    name_tokens,
    norm_alnum,
    reference_tokens,
)


@dataclass(slots=True)
class Candidate:
    pos: int
    score: float
    reason: str
    amount_cents: int  # open amount in the item currency, at match time
    is_open: bool = True
    applied_cents: int | None = None  # exact amount stated by an advice/batch file
    hint: str = ""  # exception implied by the source document


@dataclass(slots=True)
class DocumentGroup:
    """One aggregated operation: N documents settled by a single bank line."""

    kind: str  # dd_batch | card | cheque | payment_run
    account_id: str
    ymd: int
    total_cents: int
    entries: list[dict] = field(default_factory=list)


def _cents_series(df: pd.DataFrame, column: str) -> np.ndarray:
    if column not in df.columns or df.empty:
        return np.zeros(0, dtype=np.int64)
    return to_cents(df[column]).to_numpy(dtype=np.int64)


class Context:
    """Batch / payment-run index for one phase (built once, queried per line)."""

    def __init__(self, data: PhaseData, book: ItemBook, masters, settings: Settings):
        self.data = data
        self.book = book
        self.masters = masters
        self.settings = settings
        self.groups_by_amount: dict[int, list[DocumentGroup]] = defaultdict(list)
        self.groups_by_e2e: dict[str, DocumentGroup] = {}
        self._sorted_totals: list[int] = []
        self._build()
        self._sorted_totals = sorted(self.groups_by_amount)

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        self._build_direct_debits()
        self._build_cards()
        self._build_cheques()
        self._build_payment_runs()

    def _add(self, group: DocumentGroup) -> None:
        self.groups_by_amount[group.total_cents].append(group)

    def _build_direct_debits(self) -> None:
        df = self.data.documents("direct_debit_batches")
        if df.empty:
            return
        df = df.copy()
        df["amount_cents"] = _cents_series(df, "amount")
        total_col = _cents_series(df, "batch_total") if "batch_total" in df.columns else None
        for batch_id, grp in df.groupby("batch_id", sort=False):
            total = int(total_col[grp.index[0]]) if total_col is not None and len(total_col) else int(grp["amount_cents"].sum())
            group = DocumentGroup(
                kind="dd_batch",
                account_id=str(grp["account_id"].iloc[0]) if "account_id" in grp else "",
                ymd=ymd(str(grp["collection_date"].iloc[0])) if "collection_date" in grp else 0,
                total_cents=total,
                entries=grp.to_dict("records"),
            )
            self._add(group)
            if "end_to_end_id" in grp:
                for e2e in grp["end_to_end_id"].unique():
                    if e2e:
                        self.groups_by_e2e[str(e2e)] = group

    def _build_cards(self) -> None:
        df = self.data.documents("card_settlements")
        if df.empty:
            return
        df = df.copy()
        df["gross_cents"] = _cents_series(df, "gross")
        for settlement_id, grp in df.groupby("settlement_id", sort=False):
            self._add(
                DocumentGroup(
                    kind="card",
                    account_id=str(grp["account_id"].iloc[0]) if "account_id" in grp else "",
                    ymd=ymd(str(grp["settlement_date"].iloc[0])) if "settlement_date" in grp else 0,
                    total_cents=int(grp["gross_cents"].sum()),
                    entries=grp.to_dict("records"),
                )
            )

    def _build_cheques(self) -> None:
        df = self.data.documents("cheque_deposits")
        if df.empty:
            return
        df = df.copy()
        df["amount_cents"] = _cents_series(df, "amount")
        for deposit_id, grp in df.groupby("deposit_id", sort=False):
            total = int(_cents_series(grp, "deposit_total")[0]) if "deposit_total" in grp else int(grp["amount_cents"].sum())
            self._add(
                DocumentGroup(
                    kind="cheque",
                    account_id=str(grp["account_id"].iloc[0]) if "account_id" in grp else "",
                    ymd=ymd(str(grp["deposit_date"].iloc[0])) if "deposit_date" in grp else 0,
                    total_cents=total,
                    entries=grp.to_dict("records"),
                )
            )

    def _build_payment_runs(self) -> None:
        df = self.data.documents("payment_runs")
        if df.empty:
            return
        df = df.copy()
        df["transfer_cents"] = _cents_series(df, "transfer_amount")
        # aggregated: one bank debit for the whole file
        # confirming:  one debit per maturity date
        # transfers:   one debit per beneficiary (matched by end_to_end_id)
        agg = df[df["run_type"].isin(["aggregated", "confirming"])]
        if not agg.empty:
            keys = ["run_id"] if (agg["run_type"] == "aggregated").any() else ["run_id", "execution_date"]
            for key, grp in agg.groupby(keys, sort=False):
                group = DocumentGroup(
                    kind="payment_run",
                    account_id=str(grp["debtor_account_id"].iloc[0]) if "debtor_account_id" in grp else "",
                    ymd=ymd(str(grp["execution_date"].iloc[0])) if "execution_date" in grp else 0,
                    total_cents=int(grp["transfer_cents"].sum()),
                    entries=grp.to_dict("records"),
                )
                self._add(group)
                if "end_to_end_id" in grp:
                    for e2e in grp["end_to_end_id"].unique():
                        if e2e:
                            self.groups_by_e2e[str(e2e)] = group
        for _, row in df[df["run_type"] == "transfers"].iterrows():
            e2e = str(row.get("end_to_end_id") or "")
            if not e2e:
                continue
            self.groups_by_e2e[e2e] = DocumentGroup(
                kind="payment_run",
                account_id=str(row.get("debtor_account_id") or ""),
                ymd=ymd(str(row.get("execution_date") or "")),
                total_cents=int(row.get("transfer_cents") or 0),
                entries=[row.to_dict()],
            )

    # ------------------------------------------------------------ synthesis
    def synthesize(self, txn: dict, entry: dict, kind: str) -> dict | None:
        """Turn one document of an aggregated operation into a pseudo bank line.

        The aggregated line pays N documents; each document is matched with the same
        machinery as a normal line, using its own amount and references.
        """
        common = {
            "account_id": txn.get("account_id"),
            "entity": txn.get("entity"),
            "bank": txn.get("bank"),
            "booking_date": txn.get("booking_date"),
            "value_date": txn.get("value_date"),
            "currency": txn.get("currency") or "EUR",
            "ref1": "",
            "ref2": "",
            "text": "",
            "counterparty_name": "",
            "counterparty_account": "",
            "end_to_end_id": "",
            "remittance_info": "",
            "orig_amount": "",
            "orig_currency": "",
        }
        if kind == "dd_batch":
            cents = int(entry.get("amount_cents") or 0)
            common.update(
                text=f"{entry.get('remittance_info') or ''} {entry.get('mandate_id') or ''}".strip(),
                remittance_info=str(entry.get("remittance_info") or ""),
                counterparty_name=str(entry.get("debtor_name") or ""),
                counterparty_account=str(entry.get("debtor_iban") or ""),
                end_to_end_id=str(entry.get("end_to_end_id") or ""),
                currency=str(entry.get("currency") or common["currency"]),
            )
        elif kind == "card":
            cents = int(entry.get("gross_cents") or 0)
            common.update(text=str(entry.get("order_number") or ""), currency=str(entry.get("currency") or common["currency"]))
        elif kind == "cheque":
            cents = int(entry.get("amount_cents") or 0)
            common.update(
                text=str(entry.get("stub_reference") or ""),
                counterparty_name=str(entry.get("drawer") or ""),
                currency=str(entry.get("currency") or common["currency"]),
            )
        elif kind == "payment_run":
            cents = abs(int(entry.get("transfer_cents") or 0))
            common.update(
                text=str(entry.get("document_number") or ""),
                remittance_info=str(entry.get("remittance_info") or ""),
                counterparty_name=str(entry.get("beneficiary") or ""),
                counterparty_account=str(entry.get("beneficiary_iban") or ""),
                end_to_end_id=str(entry.get("end_to_end_id") or ""),
                currency=str(entry.get("currency") or common["currency"]),
            )
        else:
            return None
        if not cents:
            return None
        common["amount_cents"] = cents
        return common

    # --------------------------------------------------------------- lookups
    def group_for(self, txn: dict, tol_cents: int) -> DocumentGroup | None:
        """Best aggregated document set for this bank line, if any."""
        e2e = str(txn.get("end_to_end_id") or "")
        if e2e and e2e in self.groups_by_e2e:
            return self.groups_by_e2e[e2e]
        target = abs(int(txn["amount_cents"]))
        booking = ymd(str(txn.get("booking_date") or ""))
        lo = bisect.bisect_left(self._sorted_totals, target - tol_cents)
        hi = bisect.bisect_right(self._sorted_totals, target + tol_cents)
        best: DocumentGroup | None = None
        best_delta = tol_cents + 1
        for total in self._sorted_totals[lo:hi]:
            for group in self.groups_by_amount[total]:
                if group.account_id and txn.get("account_id") and group.account_id != txn["account_id"]:
                    continue
                # the operation must be booked around the same time
                if group.ymd and abs(group.ymd - booking) > 10:
                    continue
                closeness = abs(group.total_cents - target)
                if closeness < best_delta:
                    best, best_delta = group, closeness
        return best


class CandidateBuilder:
    def __init__(self, book: ItemBook, context: Context, masters, settings: Settings):
        self.book = book
        self.context = context
        self.masters = masters
        self.settings = settings
        self._token_index = self._build_name_index()
        self.advices = AdviceIndex(load_advices(context.data))

    # --------------------------------------------------------- name index
    def _build_name_index(self) -> dict[str, list[tuple[str, str]]]:
        """distinctive token -> [(party_id, side)] from the customer/supplier master."""
        index: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for df, id_col, side in (
            (self.masters.customers, "customer_id", "AR"),
            (self.masters.suppliers, "supplier_id", "AP"),
        ):
            if df.empty:
                continue
            for row in df.itertuples(index=False):
                pid = getattr(row, id_col)
                names = " ".join(str(v) for v in (row.name, getattr(row, "trade_name", "")) if v)
                for tok in name_tokens(names):
                    if len(tok) >= 4:
                        index[tok].append((pid, side))
        return index

    def _parties_from_text(self, text: str, limit: int = 6) -> list[tuple[str, float]]:
        toks = name_tokens(text)
        if not toks:
            return []
        votes: dict[str, float] = defaultdict(float)
        for tok in toks:
            for pid, _side in self._token_index.get(tok, ()):  # bounded by master size
                votes[pid] += 1.0
        if not votes:
            return []
        scored = []
        for pid, hits in votes.items():
            # Coverage of the bank text's distinctive tokens; 1 token is enough for
            # long/rare names but not for "POOL".
            coverage = hits / max(len(toks), 1)
            if hits >= 2 or coverage >= 0.34:
                scored.append((pid, coverage))
        scored.sort(key=lambda p: -p[1])
        return scored[:limit]

    # --------------------------------------------------------- generation
    def build(self, txn: dict) -> list[Candidate]:
        book = self.book
        booking = ymd(str(txn.get("booking_date") or ""))
        target = abs(int(txn["amount_cents"]))
        tol = self.settings.scoring.tolerant(target)
        is_credit = int(txn["amount_cents"]) > 0
        preferred_side = "AR" if is_credit else "AP"
        txn_currency = str(txn.get("currency") or "EUR")

        def in_item_currency(item) -> int:
            """The cash amount expressed in the item currency (FX-aware)."""
            if item.currency == txn_currency:
                return target
            converted = book.fx.convert(target, txn_currency, item.currency, booking)
            return converted if converted is not None else target

        candidates: dict[int, Candidate] = {}

        def add(
            pos: int,
            score: float,
            reason: str,
            applied: int | None = None,
            hint: str = "",
        ) -> None:
            item = book.get(pos)
            if item.open_cents <= 0:
                # Closed items are still useful: DUPLICATE_PAYMENT / RETURNED.
                candidates.setdefault(
                    pos, Candidate(pos, score * 0.5, reason + ":closed", 0, False, applied, hint)
                )
                return
            if item.issue_ymd and booking and item.issue_ymd > booking:
                return  # cannot settle an item issued after the cash (drift check)
            prev = candidates.get(pos)
            if prev is None or prev.score < score:
                candidates[pos] = Candidate(pos, score, reason, item.open_cents, True, applied, hint)

        # 1) document references in the free text
        refs = reference_tokens(
            str(txn.get("text") or ""),
            str(txn.get("ref1") or ""),
            str(txn.get("ref2") or ""),
            str(txn.get("remittance_info") or ""),
            min_len=self.settings.matching.min_ref_len,
        )
        if refs:
            positions, confidences = book.by_refs(refs)
            for pos, conf in zip(positions, confidences, strict=False):
                add(int(pos), 0.75 + 0.25 * float(conf), "ref")

        # 2) aggregated documents (a batch line pays N documents exactly)
        group = self.context.group_for(txn, tol)
        if group is not None:
            for pos, score in self._resolve_group(group, target):
                add(pos, score, group.kind)
            # the group is authoritative even when the sum drifted a little
            if candidates:
                for pos in list(candidates):
                    if candidates[pos].reason == "ref":
                        candidates[pos].score = max(candidates[pos].score, 0.9)

        # 2b) remittance advice: it names the documents and the deductions
        if len(self.advices):
            payer_blob = " ".join(
                str(txn.get(k) or "") for k in ("counterparty_name", "text", "remittance_info")
            )
            advice = self.advices.find(payer_blob, target, booking, tol)
            if advice is not None:
                for entry in advice.entries:
                    refs = {norm_alnum(entry.document_number)}
                    refs.discard("")
                    positions, _ = book.by_refs(refs)
                    for pos in positions:
                        applied = abs(entry.paid_cents or entry.gross_cents)
                        add(int(pos), 0.99, "advice", applied=applied, hint=entry.hint)

        # 3) counterparty: IBAN first, then fuzzy name
        party_ids: list[str] = []
        iban = norm_alnum(str(txn.get("counterparty_account") or ""))
        if iban:
            owner = self.masters.iban_owner.get(iban)
            if owner:
                party_ids.append(owner[0])
        text_blob = " ".join(
            str(txn.get(k) or "") for k in ("counterparty_name", "text", "remittance_info", "ref1", "ref2")
        )
        for pid, sim in self._parties_from_text(text_blob):
            if pid not in party_ids:
                party_ids.append(pid)

        if party_ids:
            codes = np.unique(np.concatenate([book.party_codes_for(pid) for pid in party_ids]))
            if len(codes):
                party_positions = np.flatnonzero(np.isin(book.party_code, codes))
                # exact/close amount for a known party
                for pos in party_positions:
                    item = book.get(int(pos))
                    if item.open_cents <= 0:
                        continue
                    expected = in_item_currency(item)
                    delta = abs(item.open_cents - expected)
                    if delta <= tol:
                        add(int(pos), 0.72, "party+amount")
                    elif _looks_like_discount(item, expected, self.masters, booking):
                        add(int(pos), 0.66, "party+discount")

        # 4) amount-only fallback (in EUR, so cross-currency items are covered too)
        eur_target = book.fx.to_eur(target, txn_currency, booking)
        for pos in book.by_amount(target, tol):
            add(int(pos), self.settings.matching.amount_only_score, "amount")
        if eur_target:
            for pos in book.open_positions():
                item = book.get(int(pos))
                if item.currency == txn_currency:
                    continue
                item_eur = book.fx.to_eur(item.open_cents, item.currency, booking)
                if item_eur is not None and abs(item_eur - eur_target) <= max(0.01, 0.005 * item_eur):
                    add(int(pos), self.settings.matching.amount_only_score * 0.8, "amount_fx")

        # side preference: credits rarely settle supplier invoices and vice versa
        for cand in candidates.values():
            if book.side[cand.pos] != preferred_side:
                cand.score *= 0.45
            if cand.is_open and cand.amount_cents == target:
                cand.score += 0.05

        ranked = sorted(candidates.values(), key=lambda c: (-c.score, c.amount_cents, book.issue_ymd[c.pos]))
        return ranked[: self.settings.matching.max_candidates]

    # ---------------------------------------------------------- group -> items
    def _resolve_group(self, group: DocumentGroup, target: int) -> list[tuple[int, float]]:
        book = self.book
        out: dict[int, float] = {}
        party_cache: dict[str, str | None] = {}

        def match_entry(doc_number: str, amount_cents: int, party_hint: str | None, score: float) -> None:
            if doc_number:
                refs = {norm_alnum(doc_number), norm_alnum(doc_number).lstrip("0")}
                refs.discard("")
                positions, _ = book.by_refs(refs)
                for pos in positions:
                    out[int(pos)] = max(out.get(int(pos), 0.0), score)
                if len(out):
                    return
            if party_hint is None:
                return
            if party_hint not in party_cache:
                owner = self.masters.iban_owner.get(norm_alnum(party_hint))
                party_cache[party_hint] = owner[0] if owner else None
            pid = party_cache[party_hint]
            if not pid:
                return
            codes = book.party_codes_for(pid)
            if not len(codes):
                return
            tol = self.settings.scoring.tolerant(amount_cents)
            for pos in book.by_amount(amount_cents, tol):
                if book.party_code[int(pos)] in set(int(c) for c in codes) and book.open_cents[int(pos)] > 0:
                    out[int(pos)] = max(out.get(int(pos), 0.0), score)

        if group.kind == "dd_batch":
            for entry in group.entries:
                match_entry(
                    str(entry.get("remittance_info") or entry.get("mandate_id") or ""),
                    int(entry.get("amount_cents") or 0),
                    str(entry.get("debtor_iban") or ""),
                    0.97,
                )
        elif group.kind == "card":
            for entry in group.entries:
                match_entry(
                    str(entry.get("order_number") or ""),
                    int(entry.get("gross_cents") or 0),
                    None,
                    0.9,
                )
        elif group.kind == "cheque":
            for entry in group.entries:
                match_entry(
                    str(entry.get("stub_reference") or ""),
                    int(entry.get("amount_cents") or 0),
                    None,
                    0.9,
                )
        elif group.kind == "payment_run":
            for entry in group.entries:
                match_entry(
                    str(entry.get("document_number") or ""),
                    abs(int(entry.get("transfer_cents") or 0)),
                    str(entry.get("beneficiary_iban") or ""),
                    0.98,
                )
        return list(out.items())


def _looks_like_discount(item, target: int, masters, booking_ymd: int) -> bool:
    """Cheap pre-filter: could the cash be the item minus a terms discount?"""
    term = masters.terms.get(item.terms or "")
    if not term or not term.get("early_payment_discount_pct"):
        return False
    pct = float(term["early_payment_discount_pct"]) / 100.0
    expected = item.open_cents * (1 - pct)
    return abs(expected - target) <= max(2, item.open_cents // 200)


__all__ = ["Candidate", "CandidateBuilder", "Context", "DocumentGroup"]
