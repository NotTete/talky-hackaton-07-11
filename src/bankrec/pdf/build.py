"""Build the facts cache for one phase, incrementally, and report what was found.

    python -m bankrec pdf-facts --phase sample
    python -m bankrec pdf-facts --phase live --limit 500 --record

Why a cache: reading positions costs ~4 ms per PDF, so 19k documents are ~1.5 minutes but
re-running the whole pipeline for every experiment is wasteful. The cache is keyed by the
file's sha256, so a rebuild only parses documents that actually changed, and adding a
clause later re-parses nothing (clauses are cheap and can be recomputed from the facts).

The report is the honest part: how many documents produced a document number / a total / a
clause, and how many were flagged. A silent drop here looks like a matching bug later.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

from .facts import DocFacts, PdfFacts, cache_path, from_pdf, load, save


@dataclass(slots=True)
class BuildReport:
    phase: str
    documents: int = 0
    parsed: int = 0
    cached: int = 0
    with_number: int = 0
    with_total: int = 0
    arithmetic_ok: int = 0
    flags: dict | None = None
    clauses: dict | None = None
    seconds: float = 0.0
    path: str = ""

    def summary(self) -> str:
        flags = ", ".join(f"{k}={v}" for k, v in sorted((self.flags or {}).items())) or "none"
        clauses = ", ".join(f"{k}={v}" for k, v in sorted((self.clauses or {}).items())) or "none"
        return "\n".join(
            [
                f"phase        {self.phase}",
                f"documents    {self.documents:,}  (parsed {self.parsed:,}, cached {self.cached:,}) in {self.seconds:.1f}s",
                f"with docno   {self.with_number:,} ({self.with_number / max(self.documents, 1):.1%})",
                f"with total   {self.with_total:,} ({self.with_total / max(self.documents, 1):.1%})",
                f"arithmetic   {self.arithmetic_ok:,} ok",
                f"clauses      {clauses}",
                f"flags        {flags}",
                f"cache        {self.path}",
            ]
        )


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _one(job: tuple[str, str, str, str, str]) -> dict:
    path, entity, doc_type, sha, _ = job
    try:
        doc = from_pdf(Path(path), entity=entity, doc_type=doc_type, sha=sha)
    except Exception as exc:  # a broken PDF must not kill the phase
        doc = DocFacts(path=path, sha256=sha, entity=entity, doc_type=doc_type, flags=[f"error:{type(exc).__name__}"])
    return doc.as_row()


def _index(phase) -> list[dict]:
    path = Path(phase.dir) / "pdfs" / "index.csv"
    if not path.exists():
        return []
    rows = pl.read_csv(path, infer_schema_length=0).to_dicts()
    for row in rows:
        row["entity"] = (row.get("file") or "").split("/")[0]
    return rows


def build_phase(
    phase,
    *,
    limit: int = 0,
    workers: int | None = None,
    force: bool = False,
) -> tuple[PdfFacts, BuildReport]:
    """Parse new/changed PDFs, reuse the cache for the rest, return the facts + a report."""
    started = time.time()
    rows = _index(phase)
    if limit:
        rows = rows[:limit]
    report = BuildReport(phase=phase.name or "?", documents=len(rows))

    previous: dict[str, DocFacts] = {}
    if cache_path(phase).exists() and not force:
        previous = {d.path: d for d in load(phase).docs}

    jobs: list[tuple[str, str, str, str, str]] = []
    facts: list[DocFacts] = []
    for row in rows:
        path = Path(phase.dir) / "pdfs" / row["file"]
        if not path.exists():
            continue
        digest = _sha(path)
        old = previous.get(str(path))
        if old is not None and old.sha256 == digest:
            facts.append(old)
            report.cached += 1
            continue
        jobs.append((str(path), row["entity"], row.get("document_type") or "", digest, row.get("document_number") or ""))

    if jobs:
        workers = workers or min(os.cpu_count() or 1, 8)
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, job) for job in jobs]
            for i, future in enumerate(as_completed(futures), 1):
                facts.append(DocFacts.from_row(future.result()))
                if i % 500 == 0:
                    rate = i / max(time.time() - started, 1e-9)
                    print(f"  parsed {i:,}/{len(jobs):,}  {rate:,.0f}/s", file=sys.stderr)
        report.parsed = len(jobs)

    path = save(phase, facts)
    report.path = str(path)
    report.seconds = time.time() - started
    index = PdfFacts(facts)
    report.with_number = sum(1 for d in facts if d.document_number)
    report.with_total = sum(1 for d in facts if d.fields.get("total"))
    report.arithmetic_ok = sum(1 for d in facts if d.checks.get("net_plus_tax"))
    stats = index.stats()
    report.flags = stats["flags"]
    report.clauses = stats["clauses"]
    (path.parent / "pdf_facts.meta.json").write_text(
        json.dumps({"report": asdict(report), "stats": stats}, indent=2, ensure_ascii=False) + "\n"
    )
    return index, report


def report(phase) -> str:
    """Read the report of the last build (or say there is none)."""
    meta = Path(phase.dir) / "pdf_facts.meta.json"
    if not meta.exists():
        return f"no pdf facts for {phase.name}: run `bankrec pdf-facts --phase {phase.name}`"
    payload = json.loads(meta.read_text())
    return BuildReport(**payload["report"]).summary() + "\n\n" + json.dumps(payload["stats"], indent=2)


__all__ = ["BuildReport", "build_phase", "report"]
