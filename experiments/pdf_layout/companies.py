"""Turn layouts into rules, per company and per period.

The insight this experiment is after: a company does not "have a parser". It has a small
number of **templates** (one per ERP/bank output format), each with its own label
vocabulary, column roles and number conventions, and those templates **change over time**
(a new ERP, a rebrand, a different document engine).

So the output of this module is not "a parser"; it is a rule set per (entity, template):

    {"entity": "ES01", "template": "T01", "period": ["2021-07", "2025-06"],
     "fields": {"total":   {"labels": ["total", "total ttc"], "kind": "AMOUNT", "support": 0.99},
                "date":    {"labels": ["fecha"],               "kind": "DATE",   "support": 1.00},
                "docno":   {"labels": ["nº"],                  "kind": "DOCNO",  "support": 1.00}},
     "totals": ["base imponible", "iva", "total"],
     "style":  {"decimal": "comma", "date": "dmy/slash"}}

`support` is the share of that template's documents in which the label was found with that
kind of value, so a downstream consumer knows how much to trust a field instead of
assuming the label always exists. Drift is derived the same way: which template a company
used in which month, and when the label set or the number style changed.
"""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .fingerprints import Fingerprint, Template, cluster
from .tokens import classify, canonical_labels, summarise


@dataclass(slots=True)
class DocInfo:
    id: str
    entity: str
    period: str                      # YYYY-MM
    document_type: str
    fp: Fingerprint
    pairs: list[tuple[str, str]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


def _period(date: str) -> str:
    return (date or "")[:7] or "unknown"


def _share(counter: Counter) -> dict[str, float]:
    total = sum(counter.values()) or 1
    return {k: round(v / total, 4) for k, v in counter.most_common()}


#: ordered by preference: the first hint that matches wins over a later one at equal purity
FIELD_HINTS = {
    "docno": ("nº", "n°", "nr.", "nr", "num", "document", "factura", "facture", "invoice", "rechnung", "ref"),
    "date": ("fecha", "date", "datum"),
    "due": ("vencimiento", "échéance", "echeance", "fällig", "fellig", "due date", "due"),
    "total": ("total ttc", "total", "gesamt", "importe", "suma", "base imponible", "base", "netto", "neto", "subtotal"),
    "po": ("bestellung", "commande", "pedido", "your order", "order", "po "),
    "iban": ("iban", "bic", "cuenta", "konto", "compte", "bank"),
}
FIELD_KINDS = {
    "docno": {"DOCNO", "CODE"},
    "date": {"DATE"},
    "due": {"DATE"},
    "total": {"AMOUNT"},
    "po": {"DOCNO", "CODE"},
    "iban": {"IBAN", "BIC"},
}


def _priority(label: str, field_name: str) -> int | None:
    low = label.lower()
    # "Due date" contains "date": do not let a due-date label win the issue-date field
    if field_name == "date" and any(hint in low for hint in FIELD_HINTS["due"]):
        return None
    for i, hint in enumerate(FIELD_HINTS[field_name]):
        if hint in low:
            return i
    return None


def _infer_fields(pairs: list[tuple[str, str]], n_docs: int, vocab, name_set) -> dict:
    """Field -> label(s) + kind purity + support, guided by hints *and* by purity.

    Counting alone picks the label with the most amount values ("Base imponible") for
    every field; the hint table and the kind purity are what keep `docno`/`date`/`total`
    apart. Support is the share of the template's documents that contain the label at all.
    """
    by_label: dict[str, Counter] = defaultdict(Counter)
    docs_with_label: Counter = Counter()
    for label, value in pairs:
        low = label.strip().lower()
        by_label[low][classify(value, vocab=vocab, names=name_set)] += 1
        docs_with_label[low] += 1

    out: dict[str, dict] = {}
    for field_name, kinds in FIELD_KINDS.items():
        scored = []
        for label, counter in by_label.items():
            priority = _priority(label, field_name)
            if priority is None:
                continue
            total = sum(counter.values())
            purity = sum(counter.get(k, 0) for k in kinds) / total if total else 0.0
            scored.append((purity, docs_with_label[label], label, counter, priority))
        if not scored:
            continue
        # preference order beats raw frequency: "total ttc" > "total" > "base imponible"
        purity, seen, label, counter, _ = max(
            scored, key=lambda t: (t[0] >= 0.6, -t[4], t[0], t[1])
        )
        if purity < 0.6 or seen < max(2, 0.3 * n_docs):
            continue
        out[field_name] = {
            # `label` is the preferred one (hint priority); `labels` are all pure variants
            "label": label,
            "labels": sorted({lbl for p, s_, lbl, *_ in scored if p >= 0.6 and s_ >= max(2, 0.2 * n_docs)}),
            "kind": next(iter(kinds)),
            "support": round(purity, 3),
            "examples": [k for k in kinds if counter.get(k)],
        }
    return out


def entity_report(docs: list[DocInfo], *, threshold: float = 0.72, vocab=None, name_set=None) -> dict:
    """Templates, fields, style and drift for one entity.

    Templates are clustered **per document type** (an invoice and a bank-account notice are
    not the same layout even from the same company), then reported together.
    """
    docs = sorted(docs, key=lambda d: (d.period, d.id))
    by_type: dict[str, list[DocInfo]] = defaultdict(list)
    for d in docs:
        by_type[d.document_type].append(d)

    template_rules: list[dict] = []
    doc_template: dict[str, str] = {}
    for doc_type, subset in sorted(by_type.items()):
        templates = cluster([(d.id, d.fp) for d in subset], threshold=threshold)
        fp_to_id: dict[int, str] = {}
        for template in templates:
            for member in template.members:
                fp_to_id[id(member)] = f"{doc_type[:6].upper()}-{template.id}"

        by_template: dict[str, list[DocInfo]] = defaultdict(list)
        for d in subset:
            tid = fp_to_id.get(id(d.fp), "?")
            doc_template[d.id] = tid
            by_template[tid].append(d)

        for tid, members in sorted(by_template.items()):
            # keep only pairs whose label is a recurring label in this document type:
            # that is the dictionary-vs-identifier separation applied to *labels*
            known = {
                label.strip().lower()
                for label, _ in (p for d in subset for p in d.pairs)
                if sum(1 for d in subset if label.strip().lower() in {l.lower() for l in d.labels}) >= max(2, 0.05 * len(subset))
            }
            pairs = [(lbl, val) for d in members for lbl, val in d.pairs if lbl.strip().lower() in known]
            months = Counter(d.period for d in members)
            label_counter = Counter(label.strip().lower() for label, _ in pairs)
            template_rules.append(
                {
                    "template": tid,
                    "document_type": doc_type,
                    "documents": len(members),
                    "first": min(months),
                    "last": max(months),
                    "months": dict(sorted(months.items())),
                    "roles": list(members[0].fp.roles),
                    "labels": [label for label, _ in label_counter.most_common(40)],
                    "totals": sorted({
                        label for label, value in pairs
                        if classify(value, vocab=vocab, names=name_set) == "AMOUNT"
                        and any(h in label for h in ("total", "ttc", " ht", "base", "neto", "netto", "iva", "tva", "ust", "gesamt"))
                    }),
                    "fields": _infer_fields(pairs, len(members), vocab, name_set),
                    "style": {"decimal": members[0].fp.decimal_style, "date": members[0].fp.date_format},
                    "kinds": members[0].fp.kinds,
                }
            )

    monthly: dict[str, Counter] = defaultdict(Counter)
    for d in docs:
        monthly[d.period][doc_template[d.id]] += 1
    drift: list[dict] = []
    seen: set[str] = set()
    for month in sorted(monthly):
        total = sum(monthly[month].values())
        for tid, count in monthly[month].most_common():
            share = count / total
            if tid in seen:
                continue
            seen.add(tid)
            # a template used by one document is noise, not a layout change
            if share >= 0.1 or count >= 3:
                drift.append({"month": month, "event": f"{tid} starts", "share": round(share, 3), "documents": count})
    for template in template_rules:
        later = [m for m in sorted(monthly) if m > template["last"]]
        if later and template["documents"] >= 5:
            drift.append({"month": later[0], "event": f"{template['template']} stops", "share": 0.0, "documents": 0})

    style_by_month: dict[str, Counter] = {}
    for d in docs:
        # an unknown date format is missing evidence, not a style change
        style = f"{d.fp.decimal_style}/{d.fp.date_format}" if d.fp.date_format != "?" else d.fp.decimal_style
        style_by_month.setdefault(d.period, Counter())[style] += 1
    style_changes = []
    previous = None
    for month in sorted(style_by_month):
        current = style_by_month[month].most_common(1)[0][0]
        if previous and current != previous:
            style_changes.append({"month": month, "from": previous, "to": current})
        previous = current

    return {
        "entity": docs[0].entity,
        "documents": len(docs),
        "period": [min(d.period for d in docs), max(d.period for d in docs)],
        "templates": sorted(template_rules, key=lambda t: (-t["documents"], t["first"])),
        "drift": sorted(drift, key=lambda e: (e["month"], e["event"])),
        "style_changes": style_changes,
        "label_vocabulary": _share(Counter(label.lower() for d in docs for label in d.labels)),
    }


def cross_entity_summary(reports: list[dict]) -> dict:
    """Which field names are shared across companies (i.e. safe to canonicalise globally)."""
    per_entity_fields: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for report in reports:
        for template in report["templates"]:
            for name, spec in template["fields"].items():
                per_entity_fields[name][report["entity"]].update(spec["labels"])
    shared = {}
    for name, by_entity in per_entity_fields.items():
        variants = sorted({label for labels in by_entity.values() for label in labels})
        shared[name] = {
            "entities": sorted(by_entity),
            "variants": variants,
            "coverage": round(len(by_entity) / max(len(reports), 1), 3),
        }
    return shared


def fuzzy_field_match(label: str, variants: list[str], threshold: float = 0.8) -> str | None:
    """Match a new label to a known field variant (typos, case, punctuation)."""
    best, score = None, 0.0
    for variant in variants:
        ratio = difflib.SequenceMatcher(None, label.lower(), variant.lower()).ratio()
        if ratio > score:
            best, score = variant, ratio
    return best if score >= threshold else None


__all__ = ["DocInfo", "cross_entity_summary", "entity_report", "fuzzy_field_match", "summarise"]
