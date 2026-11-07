"""Text extraction with coordinates, then the smallest structure on top of it.

Plain `extract_text()` gives one string per page: good enough for regexes, useless for a
table. Every chunk the PDF draws also carries a position, so we reconstruct three things:

* **lines**   — chunks sharing a y (within a tolerance), ordered by x
* **columns** — x bands where chunks line up across lines (1-D clustering on x intervals)
* **pairs**   — (label, value) links, both horizontal (`N°` next to `FA250222794`) and
  vertical (`Fecha` above `02/06/2025`, which is how the Spanish template prints it)

That is the whole point: everything downstream (token typing, fuzzy layout matching,
per-company rules) is built on these three products, never on page strings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from .tokens import looks_like_value

Y_TOL = 2.5        # points: chunks whose y differ by less are on the same visual line
X_GAP = 14.0       # points: gap between x ranges that separates two columns
MIN_COL_WEIGHT = 3  # a column must be hit by at least this many chunks to survive


# --------------------------------------------------------------------- model


@dataclass(slots=True)
class Chunk:
    x: float
    y: float
    size: float
    text: str

    @property
    def x1(self) -> float:
        # crude right edge; enough for column clustering (no font metrics needed)
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

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.chunks)

    @property
    def size(self) -> float:
        return max(c.size for c in self.chunks)


@dataclass(slots=True)
class Band:
    lo: float
    hi: float
    weight: int = 0

    def contains(self, x: float, tol: float = 6.0) -> bool:
        return self.lo - tol <= x <= self.hi + tol

    @property
    def center(self) -> float:
        return (self.lo + self.hi) / 2


@dataclass(slots=True)
class Page:
    number: int
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)

    def cell(self, line: Line, band_index: int) -> str:
        band = self.bands[band_index]
        return " ".join(c.text for c in line.chunks if band.contains(c.x))

    def cells(self, line: Line) -> list[str]:
        return [self.cell(line, i) for i in range(len(self.bands))]


@dataclass(slots=True)
class PdfDoc:
    path: str
    entity: str
    document_type: str
    document_number: str
    document_date: str
    pages: list[Page]
    pairs: list[tuple[str, str]]          # (label, value) after horizontal+vertical linking
    labels: list[str]                     # candidate label vocabulary of this document
    values: list[tuple[str, str]]         # (kind, value) for every chunk we could type
    n_bands: int = 0                      # columns on the first page
    band_texts: list[list[str]] = field(default_factory=list)  # chunk texts per column

    @property
    def text(self) -> str:
        return "\n".join(ln.text for page in self.pages for ln in page.lines)


# ------------------------------------------------------------------ extraction


def _chunks(page) -> list[Chunk]:
    out: list[Chunk] = []

    def visitor(text, cm, tm, font_dict, font_size):  # pypdf callback
        piece = (text or "").strip()
        if not piece:
            return
        # text matrix position; cm is already applied by pypdf for most producers, and
        # for these generated PDFs tm alone sorts rows/columns correctly (verified).
        out.append(Chunk(x=float(tm[4]), y=float(tm[5]), size=float(font_size or 8.0), text=piece))

    try:
        page.extract_text(visitor_text=visitor)
    except Exception:
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
            lines.append(Line(y=chunk.y, chunks=[chunk]))
    for line in lines:
        line.chunks.sort(key=lambda c: c.x)
    return sorted(lines, key=lambda ln: -ln.y)


def _looks_like_value(text: str) -> bool:
    """One definition, in tokens.py: currency suffixes and thousand spaces included."""
    return looks_like_value(text)


def _find_table_header(lines: list[Line]) -> Line | None:
    """The line that names the table columns: several chunks, mostly words.

    Headers are the best column anchors a PDF gives us: they sit on one baseline, one chunk
    per column, and they repeat in every document of the same template.
    """
    best: tuple[int, Line] | None = None
    for line in lines[:18]:
        if len(line.chunks) < 3:
            continue
        numeric = sum(1 for c in line.chunks if _looks_like_value(c.text))
        if numeric * 2 > len(line.chunks):
            continue
        score = len(line.chunks)
        if best is None or score > best[0]:
            best = (score, line)
    return best[1] if best else None


def _clusters(spans: list[tuple[float, float]], gap: float) -> list[Band]:
    """Merge sorted (start, end) spans while the gap stays under `gap`."""
    if not spans:
        return []
    bands: list[Band] = []
    lo, hi, weight = spans[0][0], spans[0][1], 1
    for start, end in spans[1:]:
        if start - hi <= gap:
            hi = max(hi, end)
            weight += 1
        else:
            bands.append(Band(lo, hi, weight))
            lo, hi, weight = start, end, 1
    bands.append(Band(lo, hi, weight))
    return bands


def _columns(lines: list[Line]) -> list[Band]:
    """Column bands: header anchors when a table header exists, x-gap clustering otherwise.

    Long free-text chunks (footers, terms, "this invoice cancels and replaces …") are
    excluded from clustering: they start in the description column and extend across the
    page, which is exactly what used to merge every column into one.
    """
    header = _find_table_header(lines)
    if header is not None:
        anchors = sorted((c.x, c.x1) for c in header.chunks)
        bands = [Band(lo, hi, weight=len(header.chunks)) for lo, hi in anchors]
        # widen each band to cover values that are slightly left/right of the header
        for band in bands:
            band.lo -= 18
            band.hi += 18
        return bands

    spans = sorted((c.x, c.x1) for ln in lines for c in ln.chunks if len(c.text) <= 35)
    if not spans:
        spans = sorted((c.x, c.x1) for ln in lines for c in ln.chunks)
    bands = _clusters(spans, X_GAP)
    return [b for b in bands if b.weight >= MIN_COL_WEIGHT] or bands


def _pairs(lines: list[Line], bands: list[Band]) -> list[tuple[str, str]]:
    """Link labels to values.

    Two shapes exist in the corpus and they need different rules:

    * **horizontal** — `N°   FA250222794` (two chunks, one baseline)
    * **vertical**   — `Fecha` on one line and `02/06/2025` on the next

    A table row also looks like "text, then numbers", so the discriminator is *how many*
    numeric lines follow: a table row has three (qty, price, amount) while a field has one.
    """
    pairs: list[tuple[str, str]] = []
    n = len(lines)

    def is_value(text: str) -> bool:
        return _looks_like_value(text)

    # horizontal: adjacent (label, value) inside one baseline. A table row looks the
    # same at the first pair ("description | 10 | 11,31 | 113,12"), so a pair followed by
    # one more numeric chunk is a row, not a field.
    for line in lines:
        chunks = line.chunks
        for i in range(len(chunks) - 1):
            label, value = chunks[i].text.strip(), chunks[i + 1].text.strip()
            if is_value(label) or not is_value(value):
                continue
            if len(label.split()) > 5 or not label[:1].isalpha():
                continue
            rest = chunks[i + 2 :]
            if rest and all(is_value(c.text) for c in rest):
                continue
            pairs.append((label, value))
            break

    # vertical: one label line, exactly one value line below it
    for i in range(n - 1):
        line, nxt = lines[i], lines[i + 1]
        if len(line.chunks) != 1 or len(nxt.chunks) != 1:
            continue
        label, value = line.text.strip(), nxt.text.strip()
        if not label[:1].isalpha() or len(label.split()) > 4:
            continue
        if not is_value(value) or is_value(label):
            continue
        if abs(line.x0 - nxt.x0) > 40:
            continue
        # a table row continues with two more numeric lines afterwards
        following = lines[i + 2 : i + 4]
        if len(following) == 2 and all(len(f.chunks) == 1 and is_value(f.text) for f in following):
            continue
        pairs.append((label, value))

    return pairs


# ---------------------------------------------------------------------- entry


def read_pdf(path: Path, *, entity: str = "", document_type: str = "", document_number: str = "", document_date: str = "") -> PdfDoc:
    reader = PdfReader(str(path))
    pages: list[Page] = []
    all_lines: list[Line] = []
    all_bands: list[Band] = []

    for number, raw in enumerate(reader.pages):
        chunks = _chunks(raw)
        lines = _lines(chunks)
        bands = _columns(lines)
        page = Page(number=number, width=float(raw.mediabox.width), height=float(raw.mediabox.height), lines=lines, bands=bands)
        pages.append(page)
        all_lines += lines
        if not all_bands:
            all_bands = bands

    pairs = _pairs(all_lines, all_bands)
    labels = sorted({label for label, _ in pairs})
    band_texts = [
        [c.text for line in all_lines for c in line.chunks if band.contains(c.x)]
        for band in all_bands
    ]
    return PdfDoc(
        path=str(path),
        entity=entity,
        document_type=document_type,
        document_number=document_number,
        document_date=document_date,
        pages=pages,
        pairs=pairs,
        labels=labels,
        values=[],
        n_bands=len(all_bands),
        band_texts=band_texts,
    )


def to_dict(doc: PdfDoc) -> dict:
    return {
        "path": doc.path,
        "entity": doc.entity,
        "document_type": doc.document_type,
        "document_number": doc.document_number,
        "document_date": doc.document_date,
        "pages": [
            {
                "number": p.number,
                "bands": [[round(b.lo, 1), round(b.hi, 1), b.weight] for b in p.bands],
                "lines": [
                    {"y": round(ln.y, 1), "cells": p.cells(ln), "text": ln.text} for ln in p.lines
                ],
            }
            for p in doc.pages
        ],
        "pairs": doc.pairs,
        "labels": doc.labels,
        "n_bands": doc.n_bands,
        "band_texts": doc.band_texts,
    }


__all__ = ["Band", "Chunk", "Line", "Page", "PdfDoc", "read_pdf", "to_dict", "math"]
