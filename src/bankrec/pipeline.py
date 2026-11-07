"""process: every bank line of a phase in, one named build out.

    python -m bankrec process --phase sample --name baseline

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

from .alt import Engine
from .builds import Build, BuildWriter, Builds
from .config import Settings
from .data import Phase
from .documents import structure
from .experiments import capture_provenance
from .hooks import DecisionHook, LineDecision
from .matching import ItemIndex, Matcher
from .preprocess import AdviceIndex, DocumentContext, load_advices
from .rules import category_for
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
    engine: str = "alt",
    hooks: list[DecisionHook] | None = None,
    note: str = "",
    tags: list[str] | None = None,
) -> Build:
    """Build one submission and publish it as an immutable build artifact.

    `hooks` observe each decision (see hooks.py). They run after the matcher has decided
    and cannot change the submission: matching is chronological, and a hook that settled
    items would break that book (CLAUDE.md §7).
    """
    started = time.time()
    phase = Phase(settings, phase_name)
    builds = Builds(settings.builds_dir)

    masters = phase.masters
    items = phase.items()
    txns = phase.bank_transactions()
    if limit:
        txns = txns.head(limit)
    context = DocumentContext(phase)
    advices = AdviceIndex(load_advices(phase))
    document_report = structure(phase, context, advices.advices)
    if engine == "alt":
        # The operation-first engine (bankrec.alt): classify the line, then read the
        # document that already lists its items. See src/bankrec/alt/engine.py. The
        # baseline's DocumentContext/AdviceIndex are still built above because the build
        # report describes the *documents of the phase*, which neither engine owns.
        matcher = Engine(phase, items, masters)
    else:
        matcher = Matcher(ItemIndex(items, masters), context, advices)

    print(
        f"[{phase.name}] {len(txns):,} bank lines, {len(items):,} items "
        f"(items from: {', '.join(phase.used_item_phases or settings.item_phases)})",
        file=sys.stderr,
    )

    stats = Stats(by_reason={})
    hooks = list(hooks or [])
    total = len(txns)
    tick = max(total // 20, 1) if total > 25_000 else 0
    started_loop = time.time()

    writer = BuildWriter(builds.root, phase.name or settings.phase, name)
    with writer as out:
        out.write(SUBMISSION_COLUMNS)
        for txn in txns.iter_rows(named=True):
            stats.lines += 1
            if tick and stats.lines % tick == 0:
                elapsed = time.time() - started_loop
                rate = stats.lines / elapsed
                eta = (total - stats.lines) / rate if rate else 0
                print(f"  {stats.lines:,}/{total:,} lines  {rate:,.0f}/s  eta {eta:,.0f}s", file=sys.stderr)

            allocations = matcher.resolve(txn) if engine == "alt" else matcher.match(txn)
            if hooks:
                decision = LineDecision(
                    bank_txn_id=txn["bank_txn_id"],
                    reason=getattr(matcher, "reason", ""),
                    candidates=getattr(matcher, "candidates", 0),
                    items=[row.item_id for row in allocations],
                    amounts=[row.cents for row in allocations],
                    exceptions=[row.exception for row in allocations],
                )
                for hook in hooks:
                    hook.on_line(decision)
            # The alt engine answers with a category row of its own when a line settles
            # nothing, so the no-item branch takes its decision when there is one and
            # falls back to rules.category_for for the baseline.
            items_written = [row for row in allocations if getattr(row, "item_id", "")]
            if items_written:
                for row in items_written:
                    out.write([txn["bank_txn_id"], row.item_id, f"{row.cents / 100:.2f}", "", row.exception])
                    stats.item_rows += 1
                    key = f"item:{row.exception or 'clean'}"
                    stats.by_reason[key] = stats.by_reason.get(key, 0) + 1
            else:
                decided = next((row for row in allocations if getattr(row, "category", "")), None)
                category = decided.category if decided else category_for(txn, masters)
                out.write([txn["bank_txn_id"], "", "", category, decided.exception if decided else ""])
                stats.no_item_rows += 1
                stats.by_reason[category] = stats.by_reason.get(category, 0) + 1

        build = out.publish(item_phases=phase.used_item_phases or [])
        for hook in hooks:
            path = hook.finalize(builds.root, build.id)
            if path:
                build.trace = path
        build.note = note
        build.tags = list(tags or [])
        build.provenance = capture_provenance(settings, phase.name or settings.phase)
        build.documents = document_report.as_metrics()
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
