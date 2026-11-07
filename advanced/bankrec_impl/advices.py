"""Remittance advices (POLICIES.md §5.4).

Key accounts send an advice that lists the documents paid, the deductions and the
reason. It is the only place where a 1:N payment and a `SHORT_DISPUTE` can be known,
and it may arrive a few days after the cash.

Supported here: `.csv` (semicolon or comma), `.txt` (email-like). PDFs need an
extractor; `parse_pdf` will use `pypdf` when it is installed and otherwise returns
`None` (hook it to the parser of your choice — the index already lists every file).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .data import PhaseData
from .items import ymd
from .normalize import name_similarity, name_tokens, norm_alnum, parse_amount

_DATE_RE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})|(\d{4})-(\d{2})-(\d{2})")
_TOTAL_RE = re.compile(r"(?:total|importe|suma|gesamt|montant)[^\d]{0,20}([\d.,]+)", re.I)
_PAYMENT_RE = re.compile(r"(?:payment|pago|zahlung|paiement)[^\d]{0,40}([\d.,]+)", re.I)


def _date_ymd(value: str) -> int:
    value = value.strip()
    match = _DATE_RE.search(value)
    if not match:
        return 0
    if match.group(4):  # ISO
        y, m, d = int(match.group(4)), int(match.group(5)), int(match.group(6))
    else:
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
    reason: str = ""

    @property
    def hint(self) -> str:
        """Exception implied by the advice row (POLICIES.md §3)."""
        reason = self.reason.lower()
        if self.deduction_cents > 0:
            if any(k in reason for k in ("descuento", "pronto pago", "skonto", "discount", "escompte", "rabatt")):
                return "SHORT_DISCOUNT"
            if any(k in reason for k in ("comision", "gebuehr", "frais", "fee", "spesen", "charges")):
                return "SHORT_BANK_CHARGES"
            if any(k in reason for k in ("retencion", "retención", "withhold", "irpf")):
                return "SHORT_WITHHOLDING"
            return "SHORT_DISPUTE"
        if self.paid_cents and self.gross_cents and self.paid_cents > self.gross_cents:
            return "OVERPAYMENT"
        if self.paid_cents and self.gross_cents and self.paid_cents < self.gross_cents:
            return "PARTIAL"
        return ""


@dataclass(slots=True)
class Advice:
    path: str
    entity: str
    payer_name: str
    received_ymd: int
    currency: str = "EUR"
    payment_ymd: int = 0
    total_cents: int = 0
    entries: list[AdviceEntry] = field(default_factory=list)

    @property
    def computed_total_cents(self) -> int:
        return sum(e.paid_cents or e.gross_cents for e in self.entries)


class AdviceIndex:
    """Payer-name + amount lookup over every advice of the phase."""

    def __init__(self, advices: list[Advice]):
        self.advices = advices
        self._by_token: dict[str, list[int]] = defaultdict(list)
        for i, advice in enumerate(advices):
            for tok in name_tokens(advice.payer_name):
                if len(tok) >= 4:
                    self._by_token[tok].append(i)

    def __len__(self) -> int:
        return len(self.advices)

    def find(self, payer_text: str, amount_cents: int, booking_ymd: int, tol: int) -> Advice | None:
        toks = name_tokens(payer_text)
        if not toks:
            return None
        votes: dict[int, int] = defaultdict(int)
        for tok in toks:
            for i in self._by_token.get(tok, ()):
                votes[i] += 1
        best: tuple[float, Advice] | None = None
        for i, hits in votes.items():
            advice = self.advices[i]
            sim = hits / max(len(toks), 1)
            if name_similarity(toks, name_tokens(advice.payer_name)) < 0.5:
                continue
            total = advice.total_cents or advice.computed_total_cents
            if not total or abs(total - amount_cents) > tol:
                continue
            if booking_ymd and advice.received_ymd and abs(advice.received_ymd - booking_ymd) > 21:
                continue
            score = sim - abs(total - amount_cents) / max(total, 1)
            if best is None or score > best[0]:
                best = (score, advice)
        return best[1] if best else None


# ------------------------------------------------------------------ parsing


def _split_row(line: str) -> list[str]:
    delimiter = ";" if line.count(";") > line.count(",") else ","
    return [cell.strip() for cell in line.split(delimiter)]


# A single remittance row never exceeds a few million euros; anything larger is an
# order number / payment reference that was picked up by mistake.
MAX_ROW_CENTS = 500_000_000_00


def _money(cell: str) -> int | None:
    value = parse_amount(cell)
    if value is None or abs(value) > MAX_ROW_CENTS:
        return None
    return value


def _looks_like_decimals(cell: str) -> bool:
    return bool(re.search(r"[.,]\d{1,2}$", cell.strip()))


def _parse_document_row(cells: list[str], header: list[str] | None = None) -> AdviceEntry | None:
    """Recognise a document row. Uses the header when the layout provides one."""
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

    # Unknown layout: only trust cells that look like money (decimal separator).
    numbers = [_money(c) for c in cells[1:] if _looks_like_decimals(c)]
    numbers = [n for n in numbers if n is not None]
    if not numbers:
        return None
    gross = numbers[0]
    paid = numbers[-1] if len(numbers) >= 2 else gross
    reason = next(
        (
            cell
            for cell in cells[1:]
            if re.search(r"[a-záéíóúñ]{4,}", cell.lower())
            and not re.match(r"^[A-Z]{2,4}[- ]?\d", cell.upper())
        ),
        "",
    )
    return AdviceEntry(
        document_number=doc,
        gross_cents=gross,
        deduction_cents=max(gross - paid, 0),
        paid_cents=paid or gross,
        reason=reason,
    )


def parse_csv(path: Path) -> Advice | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    header: list[str] | None = None  # kept for layout debugging
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
    total = sum(e.paid_cents for e in entries)
    if not total:
        for line in lines:
            cells = _split_row(line)
            if cells and cells[0].upper().startswith("TOTAL"):
                nums = [n for n in (_money(c) for c in cells[1:]) if n]
                if nums:
                    total = nums[-1]
    return Advice(
        path=path.name,
        entity="",
        payer_name="",
        received_ymd=_date_ymd(meta.get("payment_date", "")),
        payment_ymd=_date_ymd(meta.get("payment_date", "")),
        currency=meta.get("currency", "EUR") or "EUR",
        total_cents=total,
        entries=entries,
    )


def parse_txt(path: Path) -> Advice | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: list[AdviceEntry] = []
    for line in text.splitlines():
        cells = re.split(r"\s{2,}|\t", line.strip())
        if len(cells) < 2:
            continue
        doc = cells[0].strip()
        if not re.match(r"^[A-Z]{2,4}[- ]?\d", doc.upper()):
            continue
        amount = None
        for cell in reversed(cells[1:]):
            amount = parse_amount(cell)
            if amount:
                break
        if amount is None:
            continue
        entries.append(AdviceEntry(document_number=doc, gross_cents=amount, paid_cents=amount))
    if not entries:
        return None
    # prefer the sum of the listed documents over a free-text "payment of X"
    total = sum(e.paid_cents for e in entries)
    match = _PAYMENT_RE.search(text)
    if match:
        candidate = _money(match.group(1))
        if candidate and abs(candidate - total) <= max(total // 100, 100):
            total = candidate
    return Advice(
        path=path.name,
        entity="",
        payer_name="",
        received_ymd=0,
        total_cents=total or sum(e.paid_cents for e in entries),
        entries=entries,
    )


def parse_pdf(path: Path) -> Advice | None:
    try:  # optional dependency
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    except Exception:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    entries = [e for e in (_parse_document_row(_split_row(ln)) for ln in lines) if e]
    if not entries:
        return None
    total = sum(e.paid_cents for e in entries)
    match = _TOTAL_RE.search(text) or _PAYMENT_RE.search(text)
    if match:
        candidate = _money(match.group(1))
        if candidate and abs(candidate - total) <= max(total // 100, 100):
            total = candidate
    return Advice(
        path=path.name,
        entity="",
        payer_name="",
        received_ymd=0,
        total_cents=total or sum(e.paid_cents for e in entries),
        entries=entries,
    )


PARSERS = {"csv": parse_csv, "txt": parse_txt, "pdf": parse_pdf}


def load_advices(data: PhaseData) -> list[Advice]:
    """Use remittance_advices/index.csv when present, else walk the folder."""
    root = data.dir / "remittance_advices"
    if not root.is_dir():
        return []
    rows: list[dict] = []
    index_path = root / "index.csv"
    if index_path.exists():
        import csv

        with index_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    else:
        rows = [
            {"file": str(p.relative_to(root)), "entity": "", "payer_name": "", "received_date": "", "format": p.suffix.lstrip(".")}
            for p in sorted(root.rglob("*"))
            if p.suffix.lstrip(".") in PARSERS
        ]

    out: list[Advice] = []
    for row in rows:
        rel = row.get("file") or ""
        suffix = (row.get("format") or Path(rel).suffix.lstrip(".")).lower()
        parser = PARSERS.get(suffix)
        if not parser or not rel:
            continue
        advice = parser(root / rel)
        if advice is None:
            continue
        advice.entity = row.get("entity") or advice.entity
        advice.payer_name = row.get("payer_name") or advice.payer_name
        advice.received_ymd = advice.received_ymd or ymd(row.get("received_date") or "")
        out.append(advice)
    return out


__all__ = ["Advice", "AdviceEntry", "AdviceIndex", "load_advices", "norm_alnum"]
