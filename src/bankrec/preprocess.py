"""Evidence that is not in `bank_transactions` at all (POLICIES.md §5.2-5.4).

A direct-debit batch, a card settlement run, a cheque deposit and our own payment runs
are each **one aggregated bank line** for many underlying documents; a remittance advice
names the documents a 1:N payment covers and, when there is a deduction, why. Neither
exists as a column anywhere in `bank_transactions` — this module builds, once per phase,
the two lookups that answer "what does this bank line's operation actually contain":

  DocumentContext  -> group_for(txn)   : the batch/run this line belongs to, if any
  AdviceIndex      -> find(...)        : the advice this line's payer sent, if any

Generation only: deciding which *items* those documents settle is `matching.py`'s job
(CLAUDE.md §5 — "generation asks could it, allocation asks does it add up").
`advanced/bankrec_impl/candidates.py` and `advices.py` solved this once already (pandas,
kept as reference per CLAUDE.md §9); this is the same evidence, built the way `data.py`
already builds everything else: polars for the bulk read, plain Python structures for
the per-line lookups.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .data import Phase, read_table, to_cents
from .text import alnum, name_similarity, name_tokens, parse_amount, reference_tokens

# ------------------------------------------------------------ batch / run groups


@dataclass(slots=True)
class DocumentGroup:
    """One aggregated operation: N documents settled by a single bank line."""

    kind: str  # dd_batch | card | cheque | payment_run
    account_id: str
    ymd: int
    total_cents: int
    entries: list[dict] = field(default_factory=list)


def _ymd(value: str) -> int:
    digits = str(value or "").replace("-", "")[:8]
    return int(digits) if digits.isdigit() else 0


class DocumentContext:
    """Batch / payment-run index for one phase. Build once, query per bank line."""

    def __init__(self, phase: Phase):
        self.groups_by_amount: dict[int, list[DocumentGroup]] = defaultdict(list)
        self.groups_by_e2e: dict[str, DocumentGroup] = {}
        # end-to-end id, alnum-normalised: camt053 lines carry it as its own column, but
        # n43/mt940 lines only ever have it embedded (sometimes truncated) in free text —
        # same lookup path as a document reference (text.reference_tokens/alnum).
        self.groups_by_e2e_token: dict[str, DocumentGroup] = {}
        self._build_direct_debits(phase)
        self._build_cards(phase)
        self._build_cheques(phase)
        self._build_payment_runs(phase)
        self._sorted_totals: list[int] = sorted(self.groups_by_amount)

    def _add(self, group: DocumentGroup) -> None:
        self.groups_by_amount[group.total_cents].append(group)

    def _add_e2e(self, e2e: str, group: DocumentGroup) -> None:
        self.groups_by_e2e[e2e] = group
        token = alnum(e2e)
        if token:
            self.groups_by_e2e_token[token] = group

    def _build_direct_debits(self, phase: Phase) -> None:
        df = phase.documents("direct_debit_batches")
        if df.is_empty():
            return
        cols = [to_cents("amount").alias("amount_cents")]
        has_total = "batch_total" in df.columns
        if has_total:
            cols.append(to_cents("batch_total").alias("batch_total_cents"))
        df = df.with_columns(cols)
        for (batch_id,), rows in df.group_by("batch_id", maintain_order=True):
            entries = list(rows.iter_rows(named=True))
            total = entries[0]["batch_total_cents"] if has_total else sum(e["amount_cents"] for e in entries)
            group = DocumentGroup(
                kind="dd_batch",
                account_id=str(entries[0].get("account_id") or ""),
                ymd=_ymd(entries[0].get("collection_date") or ""),
                total_cents=total,
                entries=entries,
            )
            self._add(group)
            for e2e in sorted({e["end_to_end_id"] for e in entries if e.get("end_to_end_id")}):
                self._add_e2e(e2e, group)

    def _build_cards(self, phase: Phase) -> None:
        df = phase.documents("card_settlements")
        if df.is_empty():
            return
        df = df.with_columns(to_cents("gross").alias("gross_cents"))
        for (settlement_id,), rows in df.group_by("settlement_id", maintain_order=True):
            entries = list(rows.iter_rows(named=True))
            self._add(
                DocumentGroup(
                    kind="card",
                    account_id=str(entries[0].get("account_id") or ""),
                    ymd=_ymd(entries[0].get("settlement_date") or ""),
                    total_cents=sum(e["gross_cents"] for e in entries),
                    entries=entries,
                )
            )

    def _build_cheques(self, phase: Phase) -> None:
        df = phase.documents("cheque_deposits")
        if df.is_empty():
            return
        cols = [to_cents("amount").alias("amount_cents")]
        has_total = "deposit_total" in df.columns
        if has_total:
            cols.append(to_cents("deposit_total").alias("deposit_total_cents"))
        df = df.with_columns(cols)
        for (deposit_id,), rows in df.group_by("deposit_id", maintain_order=True):
            entries = list(rows.iter_rows(named=True))
            total = entries[0]["deposit_total_cents"] if has_total else sum(e["amount_cents"] for e in entries)
            self._add(
                DocumentGroup(
                    kind="cheque",
                    account_id=str(entries[0].get("account_id") or ""),
                    ymd=_ymd(entries[0].get("deposit_date") or ""),
                    total_cents=total,
                    entries=entries,
                )
            )

    def _build_payment_runs(self, phase: Phase) -> None:
        df = phase.documents("payment_runs")
        if df.is_empty():
            return
        df = df.with_columns(to_cents("transfer_amount").alias("transfer_cents"))

        # aggregated: one bank debit for the whole file.
        # confirming:  one debit per maturity date (run_id + execution_date).
        # transfers:   one debit per beneficiary, keyed by end_to_end_id.
        batched = df.filter(df["run_type"].is_in(["aggregated", "confirming"]))
        if not batched.is_empty():
            keys = ["run_id"] if (batched["run_type"] == "aggregated").any() else ["run_id", "execution_date"]
            for _, rows in batched.group_by(keys, maintain_order=True):
                entries = list(rows.iter_rows(named=True))
                group = DocumentGroup(
                    kind="payment_run",
                    account_id=str(entries[0].get("debtor_account_id") or ""),
                    ymd=_ymd(entries[0].get("execution_date") or ""),
                    total_cents=sum(e["transfer_cents"] for e in entries),
                    entries=entries,
                )
                self._add(group)
                for e2e in sorted({e["end_to_end_id"] for e in entries if e.get("end_to_end_id")}):
                    self._add_e2e(e2e, group)

        transfers = df.filter(df["run_type"] == "transfers")
        if not transfers.is_empty():
            for (e2e,), rows in transfers.group_by("end_to_end_id", maintain_order=True):
                if not e2e:
                    continue
                entries = list(rows.iter_rows(named=True))
                self._add_e2e(
                    e2e,
                    DocumentGroup(
                        kind="payment_run",
                        account_id=str(entries[0].get("debtor_account_id") or ""),
                        ymd=_ymd(entries[0].get("execution_date") or ""),
                        total_cents=abs(entries[0]["transfer_cents"]),
                        entries=entries,
                    ),
                )

    # --------------------------------------------------------------- lookups
    def group_for(self, txn: dict, tol_cents: int) -> DocumentGroup | None:
        """The aggregated operation this bank line belongs to, if any."""
        e2e = str(txn.get("end_to_end_id") or "")
        if e2e and e2e in self.groups_by_e2e:
            return self.groups_by_e2e[e2e]

        # n43/mt940 lines never populate end_to_end_id as its own column, but the id
        # often survives (sometimes truncated at 16/35/38 chars) inside the free text.
        text_fields = (txn.get("text"), txn.get("ref1"), txn.get("ref2"), txn.get("remittance_info"))
        for token in sorted(reference_tokens(*(str(f or "") for f in text_fields))):
            group = self.groups_by_e2e_token.get(token)
            if group is not None:
                return group

        target = abs(int(txn["amount_cents"]))
        booking = _ymd(str(txn.get("booking_date") or ""))
        account_id = txn.get("account_id")
        lo = bisect.bisect_left(self._sorted_totals, target - tol_cents)
        hi = bisect.bisect_right(self._sorted_totals, target + tol_cents)

        best: DocumentGroup | None = None
        best_key: tuple[int, str, str] | None = None
        for total in self._sorted_totals[lo:hi]:
            for group in self.groups_by_amount[total]:
                if group.account_id and account_id and group.account_id != account_id:
                    continue
                if group.ymd and booking and abs(group.ymd - booking) > 10:
                    continue
                key = (abs(group.total_cents - target), group.kind, group.account_id)
                if best is None or key < best_key:
                    best, best_key = group, key
        return best


# ------------------------------------------------------------------- advices

_DATE_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})|(\d{4})-(\d{2})-(\d{2})")
_PAYMENT_RE = re.compile(r"(?:payment|pago|zahlung|paiement)[^\d]{0,40}([\d.,]+)", re.I)

# Formats we have no extractor for (README §18): record the index entry, parse nothing.
UNPARSED_ADVICE_FORMATS = {"pdf", "xlsx"}


def _date_ymd(value: str) -> int:
    match = _DATE_RE.search(str(value or "").strip())
    if not match:
        return 0
    if match.group(4):  # ISO: YYYY-MM-DD
        y, m, d = int(match.group(4)), int(match.group(5)), int(match.group(6))
    else:  # DD/MM/YYYY (the advice line tables, unlike every ISO table elsewhere)
        d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if y < 100:
            y += 2000
    return y * 10000 + m * 100 + d


@dataclass(slots=True)
class AdviceEntry:
    document_number: str
    gross_cents: int = 0
    deduction_cents: int = 0
    paid_cents: int = 0
    reason: str = ""  # raw text; classifying it into an exception is rules.py's job


@dataclass(slots=True)
class Advice:
    path: str
    entity: str
    payer_name: str
    received_ymd: int
    total_cents: int = 0
    entries: list[AdviceEntry] = field(default_factory=list)

    @property
    def computed_total_cents(self) -> int:
        return sum(e.paid_cents or e.gross_cents for e in self.entries)


def _money(cell: str) -> int | None:
    return parse_amount(cell)


def _split_row(line: str) -> list[str]:
    delimiter = ";" if line.count(";") > line.count(",") else ","
    return [cell.strip() for cell in line.split(delimiter)]


def _parse_document_row(cells: list[str], header: list[str] | None) -> AdviceEntry | None:
    """Recognise one document row of an advice line table (header-aware when possible)."""
    if not cells or not cells[0]:
        return None
    doc = cells[0]
    if not re.match(r"^[A-Z]{2,4}[- ]?\d", doc.upper()):
        return None

    columns = {name: i for i, name in enumerate(header or [])}

    def col(*names: str) -> int | None:
        for name in names:
            if name in columns:
                return columns[name]
        return None

    gross_i = col("gross", "importe", "brutto", "montant")
    paid_i = col("paid", "pagado", "net", "importe pagado")
    ded_i = col("discount_or_deduction", "deduction", "descuento", "abzug")
    reason_i = col("reason", "motivo", "grund", "motif")

    if gross_i is not None and gross_i < len(cells):
        gross = _money(cells[gross_i]) or 0
        paid = _money(cells[paid_i]) if paid_i is not None and paid_i < len(cells) else None
        ded = _money(cells[ded_i]) if ded_i is not None and ded_i < len(cells) else None
        reason = cells[reason_i] if reason_i is not None and reason_i < len(cells) else ""
        if paid is None:
            paid = gross - abs(ded or 0) if ded else gross
        return AdviceEntry(
            document_number=doc,
            gross_cents=gross,
            deduction_cents=abs(ded) if ded is not None else max(gross - paid, 0),
            paid_cents=paid if paid else gross,
            reason=reason,
        )

    # Unknown layout: only trust cells that look like money (a decimal separator).
    numbers = [n for c in cells[1:] if re.search(r"[.,]\d{1,2}$", c) and (n := _money(c)) is not None]
    if not numbers:
        return None
    gross, paid = numbers[0], (numbers[-1] if len(numbers) >= 2 else numbers[0])
    return AdviceEntry(document_number=doc, gross_cents=gross, deduction_cents=max(gross - paid, 0), paid_cents=paid or gross)


def _parse_advice_csv(path: Path) -> Advice | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    header: list[str] | None = None
    entries: list[AdviceEntry] = []
    meta: dict[str, str] = {}
    for line in lines:
        cells = _split_row(line)
        lowered = [c.lower() for c in cells]
        if any("document" in c for c in lowered) and any("paid" in c or "importe" in c for c in lowered):
            header = lowered
            continue
        if len(cells) >= 2 and cells[0].lower() in {"payment_ref", "referencia", "ref"}:
            meta = dict(zip(cells[::2], cells[1::2], strict=False))
            continue
        if cells[0].upper().startswith("TOTAL"):
            continue
        entry = _parse_document_row(cells, header)
        if entry:
            entries.append(entry)
    if not entries:
        return None
    return Advice(
        path=path.name,
        entity="",
        payer_name="",
        received_ymd=_date_ymd(meta.get("payment_date", "")),
        total_cents=sum(e.paid_cents for e in entries),
        entries=entries,
    )


def _parse_advice_txt(path: Path) -> Advice | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: list[AdviceEntry] = []
    for line in text.splitlines():
        cells = re.split(r"\s{2,}|\t", line.strip())
        if len(cells) < 2:
            continue
        doc = cells[0].strip()
        if not re.match(r"^[A-Z]{2,4}[- ]?\d", doc.upper()):
            continue
        amount = next((a for c in reversed(cells[1:]) if (a := _money(c))), None)
        if amount is None:
            continue
        entries.append(AdviceEntry(document_number=doc, gross_cents=amount, paid_cents=amount))
    if not entries:
        return None
    total = sum(e.paid_cents for e in entries)
    match = _PAYMENT_RE.search(text)
    if match:
        candidate = _money(match.group(1))
        if candidate and abs(candidate - total) <= max(total // 100, 100):
            total = candidate
    return Advice(path=path.name, entity="", payer_name="", received_ymd=0, total_cents=total, entries=entries)


_ADVICE_PARSERS = {"csv": _parse_advice_csv, "txt": _parse_advice_txt}


def load_advices(phase: Phase) -> list[Advice]:
    """Every remittance advice of the phase, parsed where we have a parser.

    `.pdf`/`.xlsx` entries are kept with empty `entries` (no extractor dependency is
    installed, per README §18's deliberate PDF omission — `.xlsx` hits the same wall) so
    the join key (path/entity/payer/date) survives for a future extractor instead of the
    file silently disappearing from the index.
    """
    root = phase.dir / "remittance_advices"
    index_path = root / "index.csv"
    if not index_path.exists():
        return []
    index = read_table(index_path)

    out: list[Advice] = []
    for row in index.iter_rows(named=True):
        rel = row.get("file") or ""
        fmt = (row.get("format") or Path(rel).suffix.lstrip(".")).lower()
        if not rel:
            continue
        if fmt in UNPARSED_ADVICE_FORMATS:
            out.append(Advice(path=rel, entity=row.get("entity") or "", payer_name=row.get("payer_name") or "", received_ymd=_ymd(row.get("received_date") or "")))
            continue
        parser = _ADVICE_PARSERS.get(fmt)
        if not parser:
            continue
        advice = parser(root / rel)
        if advice is None:
            continue
        advice.path = rel
        advice.entity = row.get("entity") or advice.entity
        advice.payer_name = row.get("payer_name") or advice.payer_name
        advice.received_ymd = advice.received_ymd or _ymd(row.get("received_date") or "")
        out.append(advice)
    return out


class AdviceIndex:
    """Payer-name + amount lookup over every parsed advice of the phase."""

    def __init__(self, advices: list[Advice]):
        self.advices = advices
        self._by_token: dict[str, list[int]] = defaultdict(list)
        for i, advice in enumerate(advices):
            for tok in sorted(name_tokens(advice.payer_name)):
                if len(tok) >= 4:
                    self._by_token[tok].append(i)

    def __len__(self) -> int:
        return len(self.advices)

    def find(self, payer_text: str, amount_cents: int, booking_ymd: int, tol: int) -> Advice | None:
        """Best advice whose payer name and total match this bank line, if any."""
        tokens = name_tokens(payer_text)
        if not tokens:
            return None
        votes: dict[int, int] = defaultdict(int)
        for tok in tokens:
            for i in self._by_token.get(tok, ()):
                votes[i] += 1

        best: tuple[float, int] | None = None
        for i in sorted(votes):
            advice = self.advices[i]
            if not advice.entries:
                continue
            if name_similarity(tokens, name_tokens(advice.payer_name)) < 0.5:
                continue
            total = advice.total_cents or advice.computed_total_cents
            if not total or abs(total - amount_cents) > tol:
                continue
            if booking_ymd and advice.received_ymd and abs(advice.received_ymd - booking_ymd) > 21:
                continue
            score = votes[i] - abs(total - amount_cents) / max(total, 1)
            if best is None or (score, -i) > (best[0], -best[1]):
                best = (score, i)
        return self.advices[best[1]] if best else None


__all__ = ["DocumentGroup", "DocumentContext", "AdviceEntry", "Advice", "AdviceIndex", "load_advices"]
