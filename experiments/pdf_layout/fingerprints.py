"""Layout fingerprints and a fuzzy similarity between them.

A layout is compared on four cheap, language-independent facts:

1. **role sequence** — the kind of token that dominates each column
   (`LABEL | DOCNO | LABEL | DATE | LABEL | AMOUNT | …`)
2. **label vocabulary** — canonicalised field names (`client`, `base imponible`, `total ttc`)
3. **totals vocabulary** — the labels whose value is an amount (the accounting skeleton)
4. **column count / geometry** — how many bands and where the last one starts

Weighting is deliberate: labels vary with language and drift, roles and totals do not.
That is what makes `FACTURE` (FR), `FACTURA` (ES) and `RECHNUNG` (DE) the *same* template
family while a different number of columns is a *different* template.

Similarity is a weighted average, and clustering is greedy with a threshold. Both are
crude on purpose: this is an experiment about whether structure alone is enough to group
documents, not a production classifier. The threshold is exposed so it can be measured.
"""

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass, field

from .tokens import classify, summarise

TOTAL_HINTS = ("total", "ttc", "ht", "base", "neto", "netto", "iva", "tva", "ust", "vat", "importe", "gesamt", "suma")


@dataclass(slots=True)
class Fingerprint:
    bands: int
    roles: list[str]
    labels: set[str]
    totals: set[str]
    kinds: dict[str, float]
    decimal_style: str
    date_format: str
    cells: int

    @property
    def role_key(self) -> str:
        return "|".join(self.roles)


def _decimal_style(values: list[str]) -> str:
    comma = sum(1 for v in values if "," in v and v.count(",") == 1 and len(v.split(",")[-1]) == 2)
    dot = sum(1 for v in values if "." in v and v.count(".") == 1 and len(v.split(".")[-1]) == 2)
    return "comma" if comma >= dot else "dot"


def _date_format(values: list[str]) -> str:
    for v in values:
        if len(v) == 10 and v[2] == "/" and v[5] == "/":
            return "dmy/slash"
        if len(v) == 10 and v[2] == "." and v[5] == ".":
            return "dmy/dot"
        if len(v) == 10 and v[4] == "-":
            return "ymd"
    return "?"


def fingerprint(doc, *, labels: set[str] | None = None, vocab: Counter | None = None, names: set[str] | None = None) -> Fingerprint:
    """`doc` is a PdfDoc (or any view exposing n_bands, band_texts, pairs, labels)."""
    bands = getattr(doc, "n_bands", 0)
    band_texts = getattr(doc, "band_texts", [])
    roles: list[str] = []
    kind_counter: Counter = Counter()
    for chunks in band_texts:
        kinds = [classify(t, labels=labels, vocab=vocab, names=names) for t in chunks]
        kind_counter.update(kinds)
        roles.append(Counter(kinds).most_common(1)[0][0] if kinds else "EMPTY")
    roles = roles[:bands] if bands else roles

    values = [value for _label, value in doc.pairs]
    totals = {
        label.lower()
        for label, value in doc.pairs
        if classify(value, labels=labels, vocab=vocab, names=names) == "AMOUNT"
        and any(hint in label.lower() for hint in TOTAL_HINTS)
    }
    return Fingerprint(
        bands=bands,
        roles=roles,
        # only *known* labels define the layout: a customer address that happened to sit
        # next to an ID is not part of the template's vocabulary
        labels={
            label.lower()
            for label in doc.labels
            if labels is None or label.lower() in labels
        },
        totals=totals,
        kinds=summarise([k for k, n in kind_counter.items() for _ in range(n)]),
        decimal_style=_decimal_style(values),
        date_format=_date_format([v for v in values if len(v) == 10]),
        cells=bands,
    )


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    # containment, not Jaccard: documents of the same template differ mainly by *extra*
    # fields (a PO appears, a notice is added). Jaccard punishes the richer document and
    # fragments one template into dozens; overlap keeps a superset in its family.
    labels = _overlap(a.labels, b.labels)
    roles = difflib.SequenceMatcher(None, a.role_key, b.role_key).ratio()
    totals = _overlap(a.totals, b.totals)
    bands = 1.0 if a.bands == b.bands else max(0.0, 1 - abs(a.bands - b.bands) / max(a.bands, b.bands))
    style = 1.0 if a.decimal_style == b.decimal_style and a.date_format == b.date_format else 0.5
    return round(0.45 * labels + 0.30 * roles + 0.15 * totals + 0.06 * bands + 0.04 * style, 4)


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass(slots=True)
class Template:
    id: str
    members: list[Fingerprint] = field(default_factory=list)
    labels: Counter = field(default_factory=Counter)
    totals: Counter = field(default_factory=Counter)
    roles: Counter = field(default_factory=Counter)

    def absorb(self, fp: Fingerprint) -> None:
        self.members.append(fp)
        self.labels.update(fp.labels)
        self.totals.update(fp.totals)
        self.roles[fp.role_key] += 1

    def signature(self) -> Fingerprint:
        """Stable signature: labels that at least half of the members share.

        Using the union of every member's labels makes a template drift as it grows (each
        new document adds words, Jaccard against the union falls, and the next document
        opens a new template). The majority view is what a company actually "has".
        """
        n = max(len(self.members), 1)
        keep = {label for label, count in self.labels.items() if count >= 0.5 * n}
        totals = {label for label, count in self.totals.items() if count >= 0.5 * n}
        roles = self.role_key.split("|") if self.roles else []
        styles = Counter(
            (m.decimal_style, m.date_format)
            for m in self.members
            if m.date_format != "?"
        ) or Counter((m.decimal_style, m.date_format) for m in self.members)
        decimal, date = styles.most_common(1)[0][0] if styles else ("comma", "?")
        kinds: Counter = Counter()
        for member in self.members:
            kinds.update(member.kinds)
        total = sum(kinds.values()) or 1
        return Fingerprint(
            bands=self.members[0].bands if self.members else 0,
            roles=roles,
            labels=keep,
            totals=totals,
            kinds={k: v / total for k, v in kinds.items()},
            decimal_style=decimal,
            date_format=date,
            cells=self.members[0].cells if self.members else 0,
        )

    def as_fingerprint(self) -> Fingerprint:
        """The template's aggregate view: union of labels/totals, modal roles."""
        roles = self.role_key.split("|") if self.roles else []
        styles = Counter((m.decimal_style, m.date_format) for m in self.members)
        decimal, date = styles.most_common(1)[0][0] if styles else ("comma", "?")
        kinds: Counter = Counter()
        for member in self.members:
            kinds.update(member.kinds)
        total = sum(kinds.values()) or 1
        return Fingerprint(
            bands=self.members[0].bands if self.members else 0,
            roles=roles,
            labels=set(self.labels),
            totals=set(self.totals),
            kinds={k: v / total for k, v in kinds.items()},
            decimal_style=decimal,
            date_format=date,
            cells=self.members[0].cells if self.members else 0,
        )

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def role_key(self) -> str:
        return self.roles.most_common(1)[0][0] if self.roles else ""

    @property
    def band_count(self) -> int:
        return self.members[0].bands if self.members else 0


def cluster(items: list[tuple[str, Fingerprint]], threshold: float = 0.72) -> list[Template]:
    """Greedy clustering: first match wins, a fresh fingerprint opens a new template.

    Greedy is O(n·k) and depends on input order, so callers sort by date first: templates
    then form in the order the company actually started using them (which is also what
    makes drift readable).
    """
    templates: list[Template] = []
    for doc_id, fp in items:
        best, best_score = None, 0.0
        for template in templates:
            score = similarity(fp, template.signature())
            if score > best_score:
                best, best_score = template, score
        if best is not None and best_score >= threshold:
            best.absorb(fp)
        else:
            template = Template(id=f"T{len(templates) + 1:02d}")
            template.absorb(fp)
            templates.append(template)
    return templates


__all__ = ["Fingerprint", "Template", "cluster", "fingerprint", "similarity"]
