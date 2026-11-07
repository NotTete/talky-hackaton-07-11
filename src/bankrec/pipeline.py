"""The loop: every bank line of a phase in, one submission CSV out.

    python -m bankrec run --phase sample          # 1 month, a few seconds
    python -m bankrec run --phase train           # 4 years (~1.5M lines, minutes)
    python -m bankrec score submissions/submission_train.csv.gz --phase train

Every bank line must appear in the output: lines you omit count as wrong, so the
pipeline always writes at least one row per line (an item row, or a category row).
"""

from __future__ import annotations

import csv
import gzip
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .data import Phase
from .matching import ItemIndex, Matcher
from .rules import category_for
from .schema import SUBMISSION_COLUMNS


@dataclass(slots=True)
class Stats:
    phase: str
    lines: int = 0
    item_rows: int = 0
    no_item_rows: int = 0
    seconds: float = 0.0
    output: Path | None = None
    by_reason: dict[str, int] = field(default_factory=dict)


def _open(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", newline="")
    return path.open("wt", encoding="utf-8", newline="")


def _money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def run(settings: Settings, phase_name: str | None = None) -> Stats:
    started = time.time()
    phase = Phase(settings, phase_name)

    masters = phase.masters
    items = phase.items()
    txns = phase.bank_transactions()
    stats = Stats(phase=phase.name or settings.phase, output=settings.submission_path(phase.name))

    matcher = Matcher(ItemIndex(items, masters))
    print(
        f"[{phase.name}] {len(txns):,} bank lines, {len(items):,} items "
        f"({', '.join(settings.item_phases)}) -> {stats.output}",
        file=sys.stderr,
    )

    with _open(stats.output) as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(SUBMISSION_COLUMNS)

        for txn in txns.iter_rows(named=True):
            stats.lines += 1
            allocations = matcher.match(txn)

            if allocations:
                for row in allocations:
                    writer.writerow([txn["bank_txn_id"], row.item_id, _money(row.cents), "", row.exception])
                    stats.item_rows += 1
                    key = f"item:{row.exception or 'clean'}"
                    stats.by_reason[key] = stats.by_reason.get(key, 0) + 1
            else:
                # TODO: match the documents of aggregated operations before giving up
                # (direct_debit_batches, card_settlements, cheque_deposits, payment_runs)
                category = category_for(txn, masters)
                writer.writerow([txn["bank_txn_id"], "", "", category, ""])
                stats.no_item_rows += 1
                stats.by_reason[category] = stats.by_reason.get(category, 0) + 1

    stats.seconds = time.time() - started
    print(
        f"[{phase.name}] {stats.lines:,} lines -> {stats.item_rows:,} item rows "
        f"+ {stats.no_item_rows:,} category rows in {stats.seconds:.1f}s",
        file=sys.stderr,
    )
    return stats


__all__ = ["Stats", "run"]
