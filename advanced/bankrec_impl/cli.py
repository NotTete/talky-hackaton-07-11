"""Command line entry point: `python -m bankrec <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, load_settings
from .data import PhaseData, read_table
from .evaluate import format_segments, score_file
from .pipeline import build_submission
from .schema import CATEGORIES, EXCEPTIONS, SUBMISSION_COLUMNS, validate_row, SubmissionRow


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="path to settings.toml")
    parser.add_argument("--data", type=Path, default=None, help="data root (default: config paths.data_dir)")
    parser.add_argument("--phase", default=None, help="sample | train | live | final")
    parser.add_argument("--item-phases", default=None, help="comma separated, e.g. train,live")


def _settings(args: argparse.Namespace):
    s = load_settings(args.config)
    if args.data:
        s.paths.data_dir = args.data
    if args.phase:
        s.run.phase = args.phase
    if args.item_phases:
        s.run.item_phases = [p.strip() for p in args.item_phases.split(",") if p.strip()]
    return s


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if args.limit:
        settings.run.limit_txns = args.limit
    if args.out:
        settings.paths.submissions_dir = Path(args.out).parent
        settings.paths.submissions_dir.mkdir(parents=True, exist_ok=True)
    stats = build_submission(settings, args.phase)
    if args.out:
        produced = settings.submission_path(args.phase)
        Path(args.out).write_bytes(produced.read_bytes())
    print(json.dumps(stats.as_dict(), indent=2))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    settings = _settings(args)
    report = score_file(Path(args.submission), settings, args.phase)
    print(report.summary())
    if report.ok:
        segments = format_segments(report)
        if segments:
            print(segments)
        if args.json:
            print(json.dumps({"metrics": report.metrics, "segments": report.segments}, indent=2))
    return 0 if report.ok else 1


def cmd_validate(args: argparse.Namespace) -> int:
    settings = _settings(args)
    path = Path(args.submission)
    phase = args.phase or settings.run.phase
    txns = set(read_table(PhaseData(settings.data_dir, phase).dir / "bank_transactions.csv.gz")["bank_txn_id"])
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    no_item: set[str] = set()
    with_item: set[str] = set()
    n = 0
    with path.open("rt", encoding="utf-8-sig", newline="") as fh:
        import csv

        reader = csv.reader(fh)
        header = [h.strip().lower() for h in next(reader, [])]
        if header != SUBMISSION_COLUMNS:
            print(f"bad header: {header} (expected {SUBMISSION_COLUMNS})")
            return 2
        for lineno, row in enumerate(reader, start=2):
            if not row or not any(row):
                continue
            n += 1
            if len(row) != 5:
                errors.append(f"row {lineno}: expected 5 fields, got {len(row)}")
                continue
            rec = SubmissionRow(*(cell.strip() for cell in row))
            errors.extend(f"row {lineno}: {msg}" for msg in validate_row(rec, phase_txns=txns))
            if rec.item_id:
                key = (rec.bank_txn_id, rec.item_id)
                if key in seen:
                    errors.append(f"row {lineno}: duplicate ({rec.bank_txn_id}, {rec.item_id})")
                seen.add(key)
                with_item.add(rec.bank_txn_id)
            else:
                if rec.bank_txn_id in no_item:
                    errors.append(f"row {lineno}: duplicate no-item row for {rec.bank_txn_id}")
                no_item.add(rec.bank_txn_id)
            if rec.exception and rec.exception not in EXCEPTIONS:
                errors.append(f"row {lineno}: unknown exception {rec.exception}")
            if rec.category and rec.category not in CATEGORIES:
                errors.append(f"row {lineno}: unknown category {rec.category}")
    for txn in with_item & no_item:
        errors.append(f"{txn}: has both item rows and a no-item row")
    covered = len(with_item | no_item)
    print(f"{n:,} rows, {covered:,}/{len(txns):,} bank lines covered")
    if errors:
        print(f"{len(errors)} error(s):")
        for err in errors[:50]:
            print("  " + err)
        return 1
    print("OK: the official scorer should accept this file")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    from .submit import submit

    result = submit(args.phase, Path(args.submission))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    root = settings.data_dir
    print(f"bankrec {__version__}")
    print(f"repo      {REPO_ROOT}")
    print(f"data root {root}  ({'ok' if root.exists() else 'MISSING'})")
    if not root.exists():
        print("  -> ./scripts/download_data.sh")
        return 1
    rows = []
    for phase in ("sample", "train", "live", "final"):
        d = root / phase
        txns = d / "bank_transactions.csv.gz"
        rows.append(
            {
                "phase": phase,
                "dir": d.exists(),
                "bank_txns": txns.exists(),
                "golden": (d / "golden_allocations.csv.gz").exists(),
                "open_items": (d / "open_items_at_start.csv.gz").exists(),
            }
        )
    for row in rows:
        print("  " + json.dumps(row))
    missing_masters = [n for n in ("customers", "suppliers", "bank_accounts", "payment_terms", "fx_rates", "categories") if not (root / "masters" / f"{n}.csv").exists()]
    print(f"masters   {'ok' if not missing_masters else 'missing: ' + ', '.join(missing_masters)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bankrec", description="Talky x Anthropic — bank reconciliation at scale")
    p.add_argument("--version", action="version", version=f"bankrec {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="build a submission for a phase")
    _add_common(run)
    run.add_argument("--limit", type=int, default=0, help="only the first N bank lines (dev)")
    run.add_argument("--out", type=Path, default=None)
    run.set_defaults(func=cmd_run)

    score = sub.add_parser("score", help="score a submission on TRAIN/sample with the official scorer")
    score.add_argument("submission", type=Path)
    _add_common(score)
    score.add_argument("--json", action="store_true")
    score.set_defaults(func=cmd_score)

    validate = sub.add_parser("validate", help="pre-flight a submission before uploading")
    validate.add_argument("submission", type=Path)
    _add_common(validate)
    validate.set_defaults(func=cmd_validate)

    submit = sub.add_parser("submit", help="upload to LIVE/FINAL and wait for the score")
    submit.add_argument("phase", choices=["LIVE", "FINAL"])
    submit.add_argument("submission", type=Path)
    submit.set_defaults(func=cmd_submit)

    doctor = sub.add_parser("doctor", help="check the data layout and config")
    _add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
