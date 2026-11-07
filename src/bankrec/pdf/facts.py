"""The fact record and the lookup index the matcher reads.

A `DocFacts` is what one PDF said, in the shape a rule can use: fields (document number,
dates, amounts, terms), clauses (instalments, retention, replaces, …), checks against the
CSVs, flags when we are unsure, and provenance for every field.

`PdfFacts` is the whole phase, indexed the way the engine needs it:

    replacement_for(token)  cancelled document number -> replacement document number
    for_docno(entity, doc)  the facts of one document
    clause(item_id)         clauses attached to an item (via its document number)

Loading is lazy and cheap: the cache is one JSONL file per phase, written by `build.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import clauses as clauses_mod
from . import fields as fields_mod
from .fields import Fields


def alnum(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


@dataclass(slots=True)
class DocFacts:
    path: str = ""
    sha256: str = ""
    entity: str = ""
    doc_type: str = ""
    document_number: str = ""
    issue_date: str = ""
    fields: dict = field(default_factory=dict)
    clauses: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    chars: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.document_number or self.fields.get("total"))

    def as_row(self) -> dict:
        row = asdict(self)
        return row

    @classmethod
    def from_row(cls, row: dict) -> "DocFacts":
        known = {k: row.get(k) for k in cls.__dataclass_fields__}
        return cls(**known)


class PdfFacts:
    """Index over one phase's PDF facts. Empty when the phase has no PDFs / no cache."""

    def __init__(self, docs: list[DocFacts] | None = None):
        self.docs: list[DocFacts] = docs or []
        self._by_doc: dict[str, list[DocFacts]] = {}
        self._replaced: dict[str, str] = {}
        self._clauses: dict[str, list[dict]] = {}
        for doc in self.docs:
            if doc.document_number:
                self._by_doc.setdefault(alnum(doc.document_number), []).append(doc)
            for clause in doc.clauses:
                if clause["code"] == "replaces" and not clause.get("negated") and clause.get("document"):
                    self._replaced.setdefault(alnum(clause["document"]), alnum(doc.document_number))

    # ------------------------------------------------------------------ api
    def __len__(self) -> int:
        return len(self.docs)

    def for_docno(self, document_number: str) -> DocFacts | None:
        hits = self._by_doc.get(alnum(document_number)) or []
        return hits[0] if hits else None

    def replacement_for(self, token: str) -> str:
        """Cancelled document number -> the number of the invoice that replaces it."""
        return self._replaced.get(alnum(token), "")

    def for_item(self, item: dict) -> DocFacts | None:
        """Facts of the PDF that belongs to one item row (same entity + document number)."""
        doc = self.for_docno(item.get("document_number") or "")
        if doc and item.get("entity") and doc.entity and item["entity"] != doc.entity:
            return None
        return doc

    def clauses_for_item(self, item: dict) -> list[dict]:
        doc = self.for_item(item)
        return doc.clauses if doc else []

    def has(self, item: dict, code: str) -> bool:
        return any(c["code"] == code and not c.get("negated") for c in self.clauses_for_item(item))

    def stats(self) -> dict:
        codes: dict[str, int] = {}
        for doc in self.docs:
            for clause in doc.clauses:
                codes[clause["code"]] = codes.get(clause["code"], 0) + 1
        flagged: dict[str, int] = {}
        for doc in self.docs:
            for flag in doc.flags:
                flagged[flag] = flagged.get(flag, 0) + 1
        return {
            "documents": len(self.docs),
            "with_number": sum(1 for d in self.docs if d.document_number),
            "with_total": sum(1 for d in self.docs if d.fields.get("total")),
            "clauses": codes,
            "flags": flagged,
            "replaced": len(self._replaced),
        }


# --------------------------------------------------------------------- cache


def cache_path(phase) -> Path:
    return Path(phase.dir) / "pdf_facts.jsonl"


def load(phase) -> PdfFacts:
    path = cache_path(phase)
    if not path.exists():
        return PdfFacts()
    docs = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                docs.append(DocFacts.from_row(json.loads(line)))
    return PdfFacts(docs)


def save(phase, docs: list[DocFacts]) -> Path:
    path = cache_path(phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for doc in sorted(docs, key=lambda d: d.path):
            fh.write(json.dumps(doc.as_row(), ensure_ascii=False) + "\n")
    tmp.replace(path)
    return path


def from_pdf(path: Path, *, entity: str = "", doc_type: str = "", sha: str = "") -> DocFacts:
    """Read one PDF and produce its facts (no CSV cross-checks here: see build.py)."""
    from .read import pairs as read_pairs
    from .read import read as read_pdf
    from .read import text as read_text

    pages = read_pdf(path)
    text = read_text(pages)
    pairs = [(p.label, p.value) for p in read_pairs(pages)]
    resolved: Fields = fields_mod.resolve(pairs, text)
    seller, buyer = fields_mod.seller_buyer(pairs)
    found = clauses_mod.extract(text)
    flags: list[str] = []
    if len(text.strip()) < 40:
        flags.append("no_text")
    elif len(text.strip()) < 200:
        flags.append("sparse")
    if not resolved.document_number:
        flags.append("no_docno")
    if not resolved.total:
        flags.append("no_total")
    if resolved.net and resolved.tax and resolved.total and resolved.net + resolved.tax != resolved.total:
        flags.append("arithmetic_fail")
    return DocFacts(
        path=str(path),
        sha256=sha,
        entity=entity,
        doc_type=doc_type,
        document_number=resolved.document_number,
        issue_date=resolved.issue_date,
        fields={
            "issue_date": resolved.issue_date,
            "due_date": resolved.due_date,
            "currency": resolved.currency,
            "net": resolved.net,
            "tax": resolved.tax,
            "total": resolved.total,
            "terms_days": resolved.terms_days,
            "discount_pct": resolved.discount_pct,
            "discount_days": resolved.discount_days,
            "iban": resolved.iban,
            "po": resolved.po,
            "buyer": buyer,
            "seller": seller,
            "labels": resolved.labels,
        },
        clauses=found,
        checks=resolved.checks,
        flags=flags,
        chars=len(text.strip()),
    )


__all__ = ["DocFacts", "PdfFacts", "alnum", "cache_path", "from_pdf", "load", "save"]
