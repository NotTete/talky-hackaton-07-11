"""Positions -> lines -> columns -> (label, value) pairs.

Why positions and not `extract_text()`: a page string is enough for regexes and useless
for a table. Every chunk the PDF draws carries an (x, y); grouping by y gives lines,
grouping by x gives columns, and the *line that names the columns* is the most reliable
anchor a generated invoice gives us.

The one rule that makes pairing work is the table-row discriminator: a field is a label
followed by **one** value, a table row is a description followed by **three** numbers
(quantity, price, amount). Without it, every product description becomes a field name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

Y_TOL = 2.5          # points; chunks closer than this share a visual line
X_GAP = 14.0         # points; gap that separates two columns when there is no header
MIN_HITS = 3         # a column must be hit by at least this many chunks
VALUE_RE = re.compile(
    r"^\d{1,4}(?:[.,]\d{1,3})*$"
    r"|^\d{2}[/.]\d{2}[/.]\d{4}$"
    r"|^[A-Za-z]{0,4}\d[\w./-]{3,}$"
    r"|^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$"
    r"|^\d+(?:[.,]\d+)?\s?%$",
)


@dataclass(slots=True)
class Chunk:
    x: float
    y: float
    size: float
    text: str

    @property
    def x1(self) -> float:
        return self.x + max(len(self.text) * self.size * 0.5, 2.0)


@dataclass(slots=True)
class Line:
    y: float
    chunks: list[Chunk]

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.chunks)

    @property
    def x0(self) -> float:
        return min(c.x for c in self.chunks)


@dataclass(slots=True)
class Band:
    lo: float
    hi: float
    hits: int = 0

    def contains(self, x: float, tol: float = 6.0) -> bool:
        return self.lo - tol <= x <= self.hi + tol


@dataclass(slots=True)
class Pair:
    label: str
    value: str
    y: float
    x: float


def is_value(text: str) -> bool:
    """Could this chunk be the value half of a label/value pair?"""
    t = text.strip().rstrip(":;,")
    if not t:
        return False
    return bool(VALUE_RE.match(t)) or bool(re.match(r"^[\d.,]+\s+[A-Z]{3}$", t))


def _chunks(page) -> list[Chunk]:
    out: list[Chunk] = []

    def visitor(text, cm, tm, font_dict, font_size):
        piece = (text or "").strip()
        if piece:
            out.append(Chunk(float(tm[4]), float(tm[5]), float(font_size or 8.0), piece))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:  # a broken page must not kill the document
        return out
    return out


def _lines(chunks: list[Chunk]) -> list[Line]:
    lines: list[Line] = []
    for chunk in sorted(chunks, key=lambda c: (-c.y, c.x)):
        for line in lines:
            if abs(line.y - chunk.y) <= Y_TOL:
                line.chunks.append(chunk)
                break
        else:
            lines.append(Line(chunk.y, [chunk]))
    for line in lines:
        line.chunks.sort(key=lambda c: c.x)
    return sorted(lines, key=lambda ln: -ln.y)


def _header(lines: list[Line]) -> Line | None:
    """The line naming the table columns: several chunks, mostly words."""
    best: tuple[int, Line] | None = None
    for line in lines[:18]:
        if len(line.chunks) < 3:
            continue
        if sum(1 for c in line.chunks if is_value(c.text)) * 2 > len(line.chunks):
            continue
        if best is None or len(line.chunks) > best[0]:
            best = (len(line.chunks), line)
    return best[1] if best else None


def _bands(lines: list[Line]) -> list[Band]:
    header = _header(lines)
    if header is not None:
        return [Band(c.x - 18, c.x1 + 18, len(header.chunks)) for c in header.chunks]
    spans = sorted((c.x, c.x1) for ln in lines for c in ln.chunks if len(c.text) <= 35)
    if not spans:
        return []
    bands = [Band(spans[0][0], spans[0][1], 1)]
    for start, end in spans[1:]:
        if start - bands[-1].hi <= X_GAP:
            bands[-1].hi = max(bands[-1].hi, end)
            bands[-1].hits += 1
        else:
            bands.append(Band(start, end, 1))
    return [b for b in bands if b.hits >= MIN_HITS] or bands


def _pairs(lines: list[Line]) -> list[Pair]:
    out: list[Pair] = []

    # horizontal: adjacent (label, value) on one baseline
    for line in lines:
        chunks = line.chunks
        for i in range(len(chunks) - 1):
            label, value = chunks[i].text.strip(), chunks[i + 1].text.strip()
            if is_value(label) or not is_value(value):
                continue
            if len(label.split()) > 5 or not label[:1].isalpha():
                continue
            rest = chunks[i + 2:]
            if rest and all(is_value(c.text) for c in rest):
                continue  # a table row, not a field
            out.append(Pair(label, value, line.y, chunks[i].x))
            break

    # vertical: label above value (the Spanish/French/German layouts)
    for i, line in enumerate(lines[:-1]):
        nxt = lines[i + 1]
        if len(line.chunks) != 1 or len(nxt.chunks) != 1:
            continue
        label, value = line.text.strip(), nxt.text.strip()
        if not label[:1].isalpha() or len(label.split()) > 4 or is_value(label):
            continue
        if not is_value(value) or abs(line.x0 - nxt.x0) > 40:
            continue
        following = lines[i + 2 : i + 4]
        if len(following) == 2 and all(len(f.chunks) == 1 and is_value(f.text) for f in following):
            continue  # qty / price / amount: a table row
        out.append(Pair(label, value, line.y, line.x0))
    return out


@dataclass(slots=True)
class Page:
    number: int
    lines: list[Line] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def read(path: Path) -> list[Page]:
    """Read one PDF into pages of lines/bands. No interpretation happens here."""
    pages: list[Page] = []
    try:
        reader = PdfReader(str(path))
    except Exception:
        return pages
    for number, raw in enumerate(reader.pages):
        lines = _lines(_chunks(raw))
        pages.append(Page(number=number, lines=lines, bands=_bands(lines)))
    return pages


def pairs(pages: list[Page]) -> list[Pair]:
    out: list[Pair] = []
    for page in pages:
        out.extend(_pairs(page.lines))
    return out


def text(pages: list[Page]) -> str:
    return "\n".join(page.text for page in pages)


__all__ = ["Band", "Chunk", "Line", "Page", "Pair", "is_value", "pairs", "read", "text"]
