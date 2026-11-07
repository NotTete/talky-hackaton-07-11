"""process: every bank line of a phase in, one named build out.

    python -m bankrec process --phase sample --name try

Artifacts go to `builds/` as `YYYYMMDD-HHMM-<content-hash>-<name>.csv.gz` (see
builds.py). Nothing is scored here and nothing is uploaded: `evaluate` and `push` are
separate decisions.

Every bank line must appear in the output — lines you omit count as wrong — so a line
that settles nothing still gets a category row.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from .builds import Build, BuildWriter, Builds
from .config import Settings
from .data import Phase
from .experiments import capture_provenance
from .schema import SUBMISSION_COLUMNS


@dataclass(slots=True)
class Stats:
    lines: int = 0
    item_rows: int = 0
    no_item_rows: int = 0
    seconds: float = 0.0
    by_reason: dict[str, int] | None = None


def process(
    settings: Settings,
    phase_name: str | None = None,
    *,
    limit: int = 0,
    name: str = "",
    hooks: list | None = None,  # ignored (recon2 writes its own trace)
    note: str = "",
    tags: list[str] | None = None,
    pdf_facts=None,
) -> Build:
    """Build one submission and publish it as an immutable build artifact.

    `hooks` observe each decision (see hooks.py). They run after the matcher has decided
    and cannot change the submission: matching is chronological, and a hook that settled
    items would break that book (CLAUDE.md §7).
    """
    started = time.time()
    phase = Phase(settings, phase_name)
    builds = Builds(settings.builds_dir)
    # One engine: recon2 (src/recon2, docs/recon2/DESIGN.md). The old bankrec.alt engine was
    # removed; recon2 reads the phase from recon2.model.DATA (env RECON2_DATA).
    from recon2 import engine as r2

    eng = r2.Engine(phase.name or phase_name or settings.phase)
    decisions = eng.run()
    if limit:
        decisions = decisions[:limit]
    ids = eng.item_ids
    import csv, gzip, tempfile
    from pathlib import Path
    from recon2 import writer as r2w

    stats = Stats(by_reason={})
    with tempfile.TemporaryDirectory() as tmp:
        guarded = Path(tmp) / "out.csv.gz"
        r2w.write(decisions, ids, guarded, {})          # recon2 guards: never a rejected file
        writer = BuildWriter(builds.root, phase.name or settings.phase, name)
        with writer as out, gzip.open(guarded, "rt", newline="") as fh:
            rd = csv.reader(fh)
            out.write(next(rd))
            for row in rd:
                out.write(row)
                if row[1]:
                    stats.item_rows += 1
                    key = f"item:{row[4] or 'clean'}"
                else:
                    stats.no_item_rows += 1
                    key = row[3]
                stats.by_reason[key] = stats.by_reason.get(key, 0) + 1
            stats.lines = len(decisions)
            build = out.publish(item_phases=[])
            build.note = note
            build.tags = list(tags or [])
            build.provenance = capture_provenance(settings, phase.name or settings.phase)
            build.documents = {}
            build.rows = stats.item_rows + stats.no_item_rows
            build.lines = stats.lines
            build.item_rows = stats.item_rows
            build.no_item_rows = stats.no_item_rows
            build.seconds = round(time.time() - started, 2)
            builds.register(build)

    print(
        f"[{phase.name}] {stats.lines:,} lines -> {stats.item_rows:,} item rows "
        f"+ {stats.no_item_rows:,} category rows in {build.seconds:.1f}s",
        file=sys.stderr,
    )
    print(build.id)
    return build


__all__ = ["Stats", "process"]
