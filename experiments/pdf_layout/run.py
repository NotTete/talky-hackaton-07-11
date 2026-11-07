#!/usr/bin/env python3
"""Run the PDF layout experiment.

    python experiments/pdf_layout/run.py --phase sample --limit 400
    python experiments/pdf_layout/run.py --phase sample --phase live --workers 8
    python experiments/pdf_layout/run.py --phase sample --threshold 0.6 --out /tmp/pdfexp

Outputs (in `out/` by default):

    docs.jsonl          one line per PDF: columns, pairs, labels, typed kinds
    template_rules.json per entity: templates, fields, support, style, drift
    cross_entity.json   which field names are shared across companies
    report.txt          the human summary printed to stdout

Everything is derived: change the threshold and templates regroup; there is no hand
written parser per entity anywhere in this folder.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments"))

import polars as pl  # noqa: E402

from pdf_layout.companies import DocInfo, cross_entity_summary, entity_report  # noqa: E402
from pdf_layout.extract import read_pdf, to_dict  # noqa: E402
from pdf_layout.fingerprints import fingerprint, similarity  # noqa: E402
from pdf_layout.tokens import build_dictionaries, classify, name_dictionary  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent / "out"


def load_index(phases: list[str], data_dir: Path, limit: int) -> list[dict]:
    rows: list[dict] = []
    for phase in phases:
        index = data_dir / phase / "pdfs" / "index.csv"
        if not index.exists():
            print(f"skip {phase}: no {index}", file=sys.stderr)
            continue
        for row in pl.read_csv(index, infer_schema_length=0).to_dicts():
            row["phase"] = phase
            rows.append(row)
    if limit:
        # stratified by entity + document_type, not the first N files
        buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            buckets[(row["file"].split("/")[0], row["document_type"])].append(row)
        per_bucket = max(limit // max(len(buckets), 1), 1)
        sampled = [r for bucket in buckets.values() for r in bucket[:per_bucket]]
        rows = sampled[:limit] if len(sampled) > limit else sampled
    return rows


def _extract_one(args: tuple[str, str, str, str, str]) -> dict:
    path, entity, doc_type, number, date = args
    try:
        doc = read_pdf(Path(path), entity=entity, document_type=doc_type, document_number=number, document_date=date)
        return to_dict(doc)
    except Exception as exc:  # a broken PDF must not kill the run
        return {"path": path, "entity": entity, "document_type": doc_type,
                "document_number": number, "document_date": date,
                "pages": [], "pairs": [], "labels": [], "n_bands": 0, "band_texts": [],
                "error": repr(exc)[:200]}


class View:
    """The subset of a PdfDoc the fingerprint needs (works on the JSON form too)."""

    __slots__ = ("n_bands", "band_texts", "pairs", "labels")

    def __init__(self, payload: dict):
        self.n_bands = payload.get("n_bands", 0)
        self.band_texts = payload.get("band_texts", [])
        self.pairs = [tuple(p) for p in payload.get("pairs", [])]
        self.labels = payload.get("labels", [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF layout experiment: structure, token typing, per-company rules")
    parser.add_argument("--phase", action="append", choices=["sample", "train-lite", "train", "live", "final"],
                        help="repeatable; default sample")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=0, help="documents per phase (0 = all)")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument("--threshold", type=float, default=0.72, help="fuzzy similarity to share a template")
    args = parser.parse_args(argv)

    phases = args.phase or ["sample"]
    args.out.mkdir(parents=True, exist_ok=True)

    rows = load_index(phases, args.data, args.limit)
    if not rows:
        print("nothing to do: no pdfs/index.csv", file=sys.stderr)
        return 1

    jobs = [(str(args.data / r["phase"] / "pdfs" / r["file"]), r["file"].split("/")[0],
             r["document_type"], r["document_number"], r["document_date"]) for r in rows]

    print(f"extracting {len(jobs):,} PDFs with {args.workers} workers ...", file=sys.stderr)
    started = time.time()
    payloads: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_extract_one, job) for job in jobs]
        for i, future in enumerate(as_completed(futures), 1):
            payloads.append(future.result())
            if i % 250 == 0:
                rate = i / (time.time() - started)
                print(f"  {i:,}/{len(jobs):,}  {rate:,.0f} PDFs/s", file=sys.stderr)
    elapsed = time.time() - started
    errors = sum("error" in p for p in payloads)

    # ---- dictionaries over the corpus: what is a word, what is a label
    class _Doc:
        def __init__(self, payload):
            self.pages = payload["pages"]
            self.pairs = [tuple(p) for p in payload["pairs"]]

    vocab, label_set = build_dictionaries([_Doc(p) for p in payloads])
    masters_dir = args.data / "masters"
    name_set: set[str] = set()
    if (masters_dir / "customers.csv").exists():
        from bankrec.data import Masters

        name_set = name_dictionary(Masters(masters_dir))

    print(
        f"extracted in {elapsed:.1f}s ({len(jobs)/elapsed:.0f} PDFs/s, {errors} errors); "
        f"vocabulary {len(vocab):,} words, {len(label_set):,} labels",
        file=sys.stderr,
    )

    # ---- fingerprint + group by entity
    by_entity: dict[str, list[DocInfo]] = defaultdict(list)
    docs_out = []
    for payload in payloads:
        view = View(payload)
        fp = fingerprint(view, labels=label_set, vocab=vocab, names=name_set)
        period = (payload.get("document_date") or "")[:7] or "unknown"
        info = DocInfo(
            id=payload["path"],
            entity=payload.get("entity") or "?",
            period=period,
            document_type=payload.get("document_type") or "?",
            fp=fp,
            pairs=view.pairs,
            labels=view.labels,
        )
        by_entity[info.entity].append(info)
        docs_out.append(
            {
                "path": payload["path"],
                "entity": info.entity,
                "period": period,
                "document_type": info.document_type,
                "n_bands": fp.bands,
                "roles": fp.roles,
                "totals": sorted(fp.totals),
                "decimal_style": fp.decimal_style,
                "date_format": fp.date_format,
                "labels": info.labels,
                "pairs": view.pairs,
                "error": payload.get("error"),
            }
        )

    reports = [entity_report(docs, threshold=args.threshold, vocab=vocab, name_set=name_set)
               for entity, docs in sorted(by_entity.items())]
    cross = cross_entity_summary(reports)

    (args.out / "docs.jsonl").write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs_out) + "\n")
    (args.out / "template_rules.json").write_text(json.dumps(reports, indent=2, ensure_ascii=False) + "\n")
    (args.out / "cross_entity.json").write_text(json.dumps(cross, indent=2, ensure_ascii=False) + "\n")

    # ---- report
    lines: list[str] = []
    lines.append(f"documents   {len(payloads):,}   ({elapsed:.1f}s, {len(jobs)/elapsed:.0f}/s, {errors} unreadable)")
    lines.append(f"vocabulary  {len(vocab):,} words, {len(label_set):,} candidate labels")
    lines.append(f"threshold   {args.threshold}")
    lines.append("")
    for report in reports:
        top = report["templates"][0]
        lines.append(
            f"{report['entity']:<6} {report['documents']:>5,} docs  {len(report['templates'])} template(s)  "
            f"period {report['period'][0]}..{report['period'][1]}  "
            f"top={top['template']} {top['documents']/report['documents']:.0%}  "
            f"style {top['style']['decimal']}/{top['style']['date']}"
        )
        for template in report["templates"][:4]:
            fields = ", ".join(
                f"{name}=<{spec['label']}>({spec['support']:.0%})" for name, spec in template["fields"].items()
            )
            lines.append(
                f"        {template['template']} n={template['documents']:>4} "
                f"{template['first']}..{template['last']}  roles={','.join(template['roles'])}  {fields}"
            )
        if report["drift"]:
            for event in report["drift"][:6]:
                lines.append(f"        drift {event['month']}  {event['event']}  share={event['share']:.0%}")
        if report["style_changes"]:
            lines.append(f"        style changes: {report['style_changes']}")
        lines.append("")
    lines.append("shared field names across companies (canonicalisation candidates):")
    for name, info in sorted(cross.items()):
        lines.append(f"  {name:<6} coverage {info['coverage']:.0%}  variants {', '.join(info['variants'][:8])}")

    text = "\n".join(lines)
    (args.out / "report.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
