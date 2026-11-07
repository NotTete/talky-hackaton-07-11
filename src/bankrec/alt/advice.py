"""Remittance advices in all four shapes the customers send them in.

`ar_short_dispute` alone is 8,318 of the sample's 35,659 golden pairs, and every one of
them is a line in a document that states the invoice, the deduction and the reason. There
is no way to reach those pairs from the bank statement: the bank text carries only the
customer's payment reference. So the advices are not an optimisation here, they are the
data.

All four formats are parsed, including the two the skeleton deliberately skipped:

* **csv** — a `document,doc_date,gross,discount_or_deduction,reason,paid` table;
* **txt** — an e-mail with a fixed-width document block;
* **pdf** — ReportLab output, so `pypdf` gives back one cell per line in reading order;
  a document row is `<number> <date> <gross> [<-deduction> <reason>] <paid>`;
* **xlsx** — read straight out of the zip (`sharedStrings.xml` + the sheet) rather than
  adding an Excel dependency for twelve files.

`pypdf` is already a transitive dependency of the environment, so none of this adds a
package. Parsing is per file and the results are reused for the whole phase.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from ..data import Phase, read_table
from ..text import alnum, fold, parse_amount

# A document number in an advice table: 2-4 letters then digits, or digits with a dash.
_DOC = re.compile(r"^(?=.*\d)[A-Z]{0,4}[-/]?\d[A-Z0-9/.\-]*$")
_DATE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
_MONEY = re.compile(r"^-?[\d.,]+$")
_TOTAL_RE = re.compile(r"(?i)\b(amount|total|importe|betrag|montant)\b")


def _ymd(value: str) -> int:
    s = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (y + 2000 if y < 100 else y) * 10000 + mo * 100 + d
    return 0


@dataclass(slots=True)
class Line:
    document: str
    gross_cents: int = 0
    deduction_cents: int = 0
    paid_cents: int = 0
    reason: str = ""


@dataclass(slots=True)
class Advice:
    path: str
    entity: str = ""
    payer_name: str = ""
    received_ymd: int = 0
    payment_ref: str = ""
    total_cents: int = 0
    lines: list[Line] = field(default_factory=list)

    @property
    def sum_paid(self) -> int:
        return sum(line.paid_cents for line in self.lines)


# ----------------------------------------------------------------- parsers


def _money(cell: str) -> int | None:
    cell = str(cell or "").strip()
    if not cell or not _MONEY.match(cell):
        return None
    return parse_amount(cell)


def _rows_to_lines(rows: list[list[str]]) -> list[Line]:
    """Header-driven table -> lines. Shared by csv and xlsx."""
    header: dict[str, int] = {}
    out: list[Line] = []
    for cells in rows:
        low = [fold(c).lower().strip() for c in cells]
        if not header and any(c.startswith("document") or c in {"documento", "beleg", "facture"} for c in low):
            header = {name: i for i, name in enumerate(low)}
            continue
        if not cells or not cells[0] or not _DOC.match(fold(cells[0])):
            continue

        def pick(*names: str) -> str:
            for name in names:
                i = header.get(name)
                if i is not None and i < len(cells):
                    return cells[i]
            return ""

        gross = _money(pick("gross", "importe", "brutto", "montant", "gross_amount")) or 0
        paid = _money(pick("paid", "pagado", "net", "zahlbetrag"))
        ded = _money(pick("discount_or_deduction", "deduction", "descuento", "abzug", "deduccion"))
        reason = pick("reason", "motivo", "grund", "motif")
        if gross == 0 and paid is None:
            numbers = [n for c in cells[1:] if (n := _money(c)) is not None]
            if not numbers:
                continue
            gross, paid = numbers[0], numbers[-1]
        if paid is None:
            paid = gross - abs(ded or 0)
        out.append(
            Line(
                document=cells[0].strip(),
                gross_cents=gross or paid,
                deduction_cents=abs(ded) if ded else max((gross or paid) - paid, 0),
                paid_cents=paid,
                reason=reason.strip(),
            )
        )
    return out


def _split_csv(line: str) -> list[str]:
    delimiter = ";" if line.count(";") > line.count(",") else ","
    return [c.strip() for c in line.split(delimiter)]


def _parse_csv(path: Path) -> Advice:
    advice = Advice(path=path.name)
    rows: list[list[str]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        cells = _split_csv(raw)
        head = fold(cells[0]).lower()
        if head in {"payment_ref", "referencia", "ref"} and len(cells) >= 2:
            meta = dict(zip(cells[::2], cells[1::2], strict=False))
            advice.payment_ref = meta.get("payment_ref", "")
            advice.received_ymd = _ymd(meta.get("payment_date", ""))
            continue
        if head.startswith("total"):
            advice.total_cents = next((n for c in reversed(cells) if (n := _money(c))), 0) or 0
            continue
        rows.append(cells)
    advice.lines = _rows_to_lines(rows)
    advice.total_cents = advice.total_cents or advice.sum_paid
    return advice


def _parse_txt(path: Path) -> Advice:
    text = path.read_text(encoding="utf-8", errors="replace")
    advice = Advice(path=path.name)
    subject = re.search(r"(?im)^subject:.*?(\d{8,})", text)
    if subject:
        advice.payment_ref = subject.group(1)
    stated = re.search(r"(?i)payment of ([\d.,]+)\s*[A-Z]{3}", text)
    if stated:
        advice.total_cents = parse_amount(stated.group(1)) or 0
    sent = re.search(r"(?i)on (\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})", text)
    if sent:
        advice.received_ymd = _ymd(sent.group(1))

    for raw in text.splitlines():
        cells = [c for c in re.split(r"\s{2,}|\t", raw.strip()) if c]
        if len(cells) < 2 or not _DOC.match(fold(cells[0])):
            continue
        numbers = [n for c in cells[1:] if (n := _money(c)) is not None]
        if not numbers:
            continue
        reason = " ".join(c for c in cells[1:] if _money(c) is None and not _DATE.match(c))
        gross, paid = numbers[0], numbers[-1]
        if len(numbers) >= 3 and numbers[1] < 0:
            gross, paid = numbers[0], numbers[-1]
        advice.lines.append(
            Line(cells[0].strip(), gross, max(gross - paid, 0), paid, reason.strip())
        )
    advice.total_cents = advice.total_cents or advice.sum_paid
    return advice


def _pdf_cells(path: Path) -> list[str]:
    from pypdf import PdfReader

    out: list[str] = []
    for page in PdfReader(str(path)).pages:
        out.extend(c.strip() for c in page.extract_text().splitlines() if c.strip())
    return out


def _parse_pdf(path: Path) -> Advice:
    """ReportLab PDFs extract one table cell per line, in reading order."""
    cells = _pdf_cells(path)
    advice = Advice(path=path.name)

    for i, cell in enumerate(cells):
        label = fold(cell).rstrip(".").strip()
        if label in {"PAYMENT REF", "REFERENCIA", "REF"} and i + 1 < len(cells):
            advice.payment_ref = cells[i + 1].strip()
        elif label == "DATE" and i + 1 < len(cells) and not advice.received_ymd:
            advice.received_ymd = _ymd(cells[i + 1])
        elif label in {"AMOUNT", "IMPORTE", "BETRAG", "MONTANT"} and i + 1 < len(cells):
            advice.total_cents = advice.total_cents or (parse_amount(cells[i + 1].split()[0]) or 0)

    # The table starts at its own header; before it sit the payer, the payee and the
    # payment reference -- and a payment reference like "REM/2025/1598" is shaped exactly
    # like a document number, so without this the header block becomes a phantom row.
    table = 0
    for i, cell in enumerate(cells):
        if fold(cell).strip() in {"DOCUMENT", "DOCUMENTO", "BELEG", "FACTURE", "NR."}:
            table = i + 1
            break

    # Document rows: a doc cell, then everything up to the next doc cell.
    starts = [
        i
        for i, c in enumerate(cells)
        if i >= table and _DOC.match(fold(c)) and not _DATE.match(c) and not _MONEY.match(c)
    ]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(cells)
        block = cells[start + 1 : end]
        numbers: list[int] = []
        reason = ""
        for cell in block:
            if _DATE.match(cell):
                continue
            # "-60.11 SHORT DELIVERY" — deduction and its reason share a cell.
            m = re.match(r"^(-?[\d.,]+)\s+(.*)$", cell)
            if m and (value := parse_amount(m.group(1))) is not None:
                numbers.append(value)
                reason = m.group(2).strip()
                continue
            value = _money(cell)
            if value is not None:
                numbers.append(value)
        if not numbers:
            continue
        gross, paid = numbers[0], numbers[-1]
        deduction = abs(next((n for n in numbers[1:-1] if n < 0), 0)) or max(gross - paid, 0)
        advice.lines.append(Line(cells[start].strip(), gross, deduction, paid, reason))
    advice.total_cents = advice.total_cents or advice.sum_paid
    return advice


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _parse_xlsx(path: Path) -> Advice:
    """Minimal xlsx reader: shared strings + the first worksheet, stdlib only."""
    rows: list[list[str]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root]
            sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
            if not sheets:
                return Advice(path=path.name)
            root = ElementTree.fromstring(zf.read(sheets[0]))
            for row in root.iter(f"{_NS}row"):
                cells: list[str] = []
                for cell in row.iter(f"{_NS}c"):
                    value = cell.find(f"{_NS}v")
                    text = "" if value is None else (value.text or "")
                    if cell.get("t") == "s" and text.isdigit() and int(text) < len(shared):
                        text = shared[int(text)]
                    elif cell.get("t") == "inlineStr":
                        node = cell.find(f"{_NS}is")
                        text = "".join(t.text or "" for t in node.iter(f"{_NS}t")) if node is not None else ""
                    cells.append(text.strip())
                rows.append(cells)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError):
        return Advice(path=path.name)

    advice = Advice(path=path.name)
    for cells in rows:
        if len(cells) >= 2 and fold(cells[0]).lower().startswith("payment_ref"):
            advice.payment_ref = cells[1]
        if len(cells) >= 2 and _TOTAL_RE.fullmatch(fold(cells[0]).lower().strip()):
            advice.total_cents = _money(cells[-1]) or advice.total_cents
    advice.lines = _rows_to_lines(rows)
    advice.total_cents = advice.total_cents or advice.sum_paid
    return advice


_PARSERS = {"csv": _parse_csv, "txt": _parse_txt, "pdf": _parse_pdf, "xlsx": _parse_xlsx}


def load(phase: Phase) -> list[Advice]:
    root = phase.dir / "remittance_advices"
    index_path = root / "index.csv"
    if not index_path.exists():
        return []
    out: list[Advice] = []
    for row in read_table(index_path).iter_rows(named=True):
        rel = row.get("file") or ""
        if not rel:
            continue
        fmt = (row.get("format") or Path(rel).suffix.lstrip(".")).lower()
        parser = _PARSERS.get(fmt)
        if parser is None:
            continue
        try:
            advice = parser(root / rel)
        except (OSError, ValueError):
            continue
        advice.path = rel
        advice.entity = row.get("entity") or ""
        advice.payer_name = row.get("payer_name") or ""
        advice.received_ymd = advice.received_ymd or _ymd(row.get("received_date") or "")
        if advice.lines:
            out.append(advice)
    return out


class AdviceIndex:
    """Find the advice behind a bank line: by its payment reference, else by payer+total.

    The payment reference is the reliable key — it is printed on the advice *and* carried
    in the bank text (`EREF+`, `CONCEPTO:`, the n43 reference). Payer+total is the
    fallback for the banks that print neither.
    """

    def __init__(self, advices: list[Advice], ledger):
        self.advices = advices
        self.by_ref: dict[str, Advice] = {}
        self._by_total: dict[int, list[Advice]] = {}
        self._by_party: dict[str, list[Advice]] = {}
        for advice in advices:
            for key in {alnum(advice.payment_ref), alnum(advice.path.rsplit("_", 1)[-1].split(".")[0])}:
                if len(key) >= 6:
                    self.by_ref.setdefault(key, advice)
            for total in {advice.total_cents, advice.sum_paid}:
                if total:
                    self._by_total.setdefault(total, []).append(advice)
            party = ledger.party_for_name(advice.payer_name)
            if party:
                self._by_party.setdefault(party, []).append(advice)
        self._totals = sorted(self._by_total)

    def by_token(self, tokens: list[str]) -> Advice | None:
        for token in tokens:
            advice = self.by_ref.get(token)
            if advice is not None:
                return advice
        return None

    def by_party_total(self, party_id: str, cents: int, ymd: int, tol: int) -> Advice | None:
        best, best_key = None, None
        for advice in self._by_party.get(party_id, ()):
            total = advice.total_cents or advice.sum_paid
            if abs(total - cents) > tol:
                continue
            if ymd and advice.received_ymd and abs(advice.received_ymd - ymd) > 21:
                continue
            key = (abs(total - cents), advice.path)
            if best_key is None or key < best_key:
                best, best_key = advice, key
        return best

    def __len__(self) -> int:
        return len(self.advices)


__all__ = ["Advice", "Line", "AdviceIndex", "load"]
