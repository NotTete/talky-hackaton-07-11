"""Command line: `python -m bankrec <command>` (or `make run`).

    run       build a submission for a phase
    score     score it on TRAIN/sample with the official scorer
    validate  pre-flight the CSV before uploading
    submit    upload to LIVE/FINAL and wait for the score
    doctor    check the data layout / config
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from dataclasses import asdict
import sys
from pathlib import Path

from . import __version__
from .config import ROOT, load_settings
from .data import Phase, read_table
from .evaluate import format_segments, score_file
from .pipeline import run
from .schema import CATEGORIES, EXCEPTIONS, SUBMISSION_COLUMNS, validate_row


def _settings(args: argparse.Namespace):
    s = load_settings(args.config)
    if args.data:
        s.data_dir = Path(args.data)
    if args.phase:
        s.phase = args.phase
    if args.item_phases:
        s.item_phases = [p.strip() for p in args.item_phases.split(",") if p.strip()]
    return s


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, help="alternative settings.toml")
    p.add_argument("--data", type=Path, help="data root (default: data/raw)")
    p.add_argument("--phase", choices=["sample", "train", "live", "final"])
    p.add_argument("--item-phases", help="comma separated item tables to load, e.g. train,live")


def cmd_run(args) -> int:
    stats = run(_settings(args), args.phase)
    print(json.dumps(asdict(stats), indent=2, default=str))
    return 0


def cmd_score(args) -> int:
    settings = _settings(args)
    report = score_file(Path(args.submission), settings, args.phase)
    print(report.summary())
    if report.ok:
        segments = format_segments(report)
        if segments:
            print(segments)
    return 0 if report.ok else 1


def cmd_validate(args) -> int:
    settings = _settings(args)
    phase = Phase(settings, args.phase)
    txns = set(read_table(phase.dir / "bank_transactions.csv.gz")["bank_txn_id"].to_list())
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    item_lines: set[str] = set()
    no_item_lines: set[str] = set()

    path = Path(args.submission)
    is_gzip = path.suffix == ".gz" or path.read_bytes()[:2] == b"\x1f\x8b"
    opener = (lambda: gzip.open(path, "rt", encoding="utf-8-sig", newline="")) if is_gzip else (
        lambda: path.open(encoding="utf-8-sig", newline="")
    )
    with opener() as fh:
        reader = csv.reader(fh)
        header = [h.strip().lower() for h in next(reader, [])]
        if header != SUBMISSION_COLUMNS:
            print(f"bad header {header}, expected {SUBMISSION_COLUMNS}")
            return 2
        rows = 0
        for lineno, cells in enumerate(reader, start=2):
            if not cells or not any(cells):
                continue
            rows += 1
            if len(cells) != 5:
                errors.append(f"row {lineno}: expected 5 fields, got {len(cells)}")
                continue
            row = tuple(c.strip() for c in cells)
            errors += [f"row {lineno}: {msg}" for msg in validate_row(row, txns)]
            txn, item = row[0], row[1]
            if item:
                if (txn, item) in seen:
                    errors.append(f"row {lineno}: duplicate ({txn}, {item})")
                seen.add((txn, item))
                item_lines.add(txn)
            else:
                if txn in no_item_lines:
                    errors.append(f"row {lineno}: two no-item rows for {txn}")
                no_item_lines.add(txn)
            if row[3] and row[3] not in CATEGORIES:
                errors.append(f"row {lineno}: unknown category {row[3]}")
            if row[4] and row[4] not in EXCEPTIONS:
                errors.append(f"row {lineno}: unknown exception {row[4]}")

    for txn in item_lines & no_item_lines:
        errors.append(f"{txn}: has both item rows and a category row")
    covered = len(item_lines | no_item_lines)
    print(f"{rows:,} rows, {covered:,}/{len(txns):,} bank lines covered")
    for err in errors[:50]:
        print("  " + err)
    if errors:
        print(f"{len(errors)} problem(s); the server would reject this file")
        return 1
    print("OK — the official scorer should accept this file")
    return 0


def cmd_submit(args) -> int:
    from .submit import submit

    print(json.dumps(submit(args.phase, Path(args.submission)), indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args) -> int:
    settings = _settings(args)
    print(f"bankrec {__version__}")
    print(f"project  {ROOT}")
    print(f"data     {settings.data_dir} ({'ok' if settings.data_dir.exists() else 'MISSING'})")
    if not settings.data_dir.exists():
        print("  -> ./scripts/download_data.sh")
        return 1
    for phase in ("sample", "train", "live", "final"):
        d = settings.data_dir / phase
        print(
            f"  {phase:<7} dir={d.exists()!s:<5} "
            f"bank_txns={(d / 'bank_transactions.csv.gz').exists()!s:<5} "
            f"golden={(d / 'golden_allocations.csv.gz').exists()!s:<5} "
            f"open_items={(d / 'open_items_at_start.csv.gz').exists()}"
        )
    masters = settings.data_dir / "masters"
    needed = ["customers", "suppliers", "bank_accounts", "payment_terms", "fx_rates", "categories"]
    missing = [n for n in needed if not (masters / f"{n}.csv").exists()]
    print(f"  masters {'ok' if not missing else 'missing: ' + ', '.join(missing)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bankrec", description="Talky x Anthropic — bank reconciliation")
    parser.add_argument("--version", action="version", version=f"bankrec {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="build a submission")
    _common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("score", help="score on TRAIN/sample (official scorer)")
    p.add_argument("submission", type=Path)
    _common(p)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("validate", help="check a submission before uploading")
    p.add_argument("submission", type=Path)
    _common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("submit", help="upload to LIVE/FINAL")
    p.add_argument("phase", choices=["LIVE", "FINAL"])
    p.add_argument("submission", type=Path)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("doctor", help="check data layout")
    _common(p)
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
