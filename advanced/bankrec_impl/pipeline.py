"""End-to-end pipeline: phase data -> submission CSV.

    python -m bankrec run --phase sample
    python -m bankrec run --phase live --item-phases train,live

Every bank line of the phase produces at least one output row (omitted lines count as
wrong in the scorer). The book is updated as we go, in booking-date order.
"""

from __future__ import annotations

import csv
import gzip
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .candidates import CandidateBuilder, Context
from .config import Settings, load_settings
from .data import PhaseData
from .items import ItemBook
from .match import Matcher
from .rules import category_for
from .schema import SUBMISSION_COLUMNS


@dataclass(slots=True)
class RunStats:
    phase: str
    txns: int = 0
    rows: int = 0
    item_rows: int = 0
    no_item_rows: int = 0
    unmatched_credits: int = 0
    unmatched_debits: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    output: Path | None = None

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "txns": self.txns,
            "rows": self.rows,
            "item_rows": self.item_rows,
            "no_item_rows": self.no_item_rows,
            "unmatched_credits": self.unmatched_credits,
            "unmatched_debits": self.unmatched_debits,
            "seconds": round(self.seconds, 2),
            "output": str(self.output) if self.output else None,
            "by_reason": self.by_reason,
        }


def _money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _open(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", newline="")
    return open(path, "wt", encoding="utf-8", newline="")


def build_submission(settings: Settings, phase: str | None = None) -> RunStats:
    phase = phase or settings.run.phase
    data = PhaseData(settings.data_dir, phase)
    if not data.dir.exists():
        raise FileNotFoundError(
            f"phase directory not found: {data.dir}\n"
            "Download the data first:  ./scripts/download_data.sh   (or set BANKREC_DATA)"
        )

    t0 = time.time()
    masters = data.masters
    items = data.items(settings.run.item_phases)
    book = ItemBook(items, masters)
    context = Context(data, book, masters, settings)
    builder = CandidateBuilder(book, context, masters, settings)
    matcher = Matcher(book, masters, settings)

    own_accounts = {
        str(r.iban): {"entity": r.entity, "account_id": r.account_id}
        for r in masters.bank_accounts.itertuples(index=False)
        if r.iban
    }

    txns = data.bank_transactions()
    if settings.run.limit_txns:
        txns = txns.head(settings.run.limit_txns)

    stats = RunStats(phase=phase, output=settings.submission_path(phase))
    print(
        f"[{phase}] {len(txns):,} bank lines | {len(items):,} items "
        f"({', '.join(settings.run.item_phases)}) -> {stats.output}",
        file=sys.stderr,
    )

    last = time.time()
    with _open(stats.output) as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(SUBMISSION_COLUMNS)

        for txn in txns.to_dict("records"):
            stats.txns += 1
            txn_id = txn["bank_txn_id"]
            # An aggregated line (SEPA batch, card settlement, cheque deposit,
            # payment run) pays N documents: expand it and match each document with
            # its own amount and references.
            group = context.group_for(txn, settings.scoring.tolerant(abs(int(txn["amount_cents"]))))
            group_rows = None
            if group is not None and len(group.entries) > 1:
                group_rows = []
                for entry in group.entries:
                    sub_txn = context.synthesize(txn, entry, group.kind)
                    if sub_txn is None:
                        continue
                    sub_alloc = matcher.allocate(sub_txn, builder.build(sub_txn))
                    if sub_alloc.matched:
                        group_rows.extend(sub_alloc.rows)
                if not group_rows:
                    group_rows = None

            if group_rows:
                stats.by_reason[f"group:{group.kind}"] = stats.by_reason.get(f"group:{group.kind}", 0) + 1
                for row in group_rows:
                    writer.writerow([txn_id, row.item_id, _money(row.cents), "", row.exception])
                    stats.rows += 1
                    stats.item_rows += 1
                continue

            candidates = builder.build(txn)
            allocation = matcher.allocate(txn, candidates)

            if allocation.matched:
                stats.by_reason[allocation.evidence] = stats.by_reason.get(allocation.evidence, 0) + 1
                for row in allocation.rows:
                    writer.writerow(
                        [txn_id, row.item_id, _money(row.cents), "", row.exception]
                    )
                    stats.rows += 1
                    stats.item_rows += 1
            else:
                category = category_for(txn, masters, settings, own_accounts=own_accounts)
                writer.writerow([txn_id, "", "", category, ""])
                stats.rows += 1
                stats.no_item_rows += 1
                if int(txn["amount_cents"]) > 0:
                    stats.unmatched_credits += 1
                else:
                    stats.unmatched_debits += 1

            if stats.txns % 25_000 == 0:
                now = time.time()
                rate = 25_000 / max(now - last, 1e-9)
                last = now
                print(
                    f"  {stats.txns:,}/{len(txns):,} lines "
                    f"({stats.item_rows:,} item rows, {rate:,.0f} lines/s)",
                    file=sys.stderr,
                )

    stats.seconds = time.time() - t0
    print(
        f"[{phase}] done: {stats.rows:,} rows in {stats.seconds:.1f}s "
        f"({stats.item_rows/stats.rows:.1%} item rows) -> {stats.output}",
        file=sys.stderr,
    )
    return stats


def main() -> None:  # convenience: python -m bankrec.pipeline --phase sample
    settings = load_settings()
    if phase := sys.argv[1] if len(sys.argv) > 1 else None:
        settings.run.phase = phase
    build_submission(settings)


__all__ = ["RunStats", "build_submission", "main"]
