"""Two ways to judge a build.

* **score** — only TRAIN and the one-month sample have a golden. When one exists, run the
  official scorer (`vendor/bankrec_score.py`) so the number is exactly the leaderboard's.
* **coverage** — LIVE/FINAL have no labels, so all you can honestly measure is structure:
  is every bank line covered, are rows unique, do codes belong to the closed lists.

Keeping them separate is deliberate: reporting a "score" for an unlabelled phase would be
a lie, and this repo is small enough that one misleading number would poison every
decision that follows.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

from .builds import Build
from .config import ROOT, Settings, load_settings
from .data import Phase
from .schema import CATEGORIES, EXCEPTIONS, SUBMISSION_COLUMNS, validate_row

SCORER_PATH = ROOT / "vendor" / "bankrec_score.py"


# --------------------------------------------------------------------- scoring


def _load_scorer():
    spec = importlib.util.spec_from_file_location("bankrec_score", SCORER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {SCORER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass(slots=True)
class ScoreReport:
    ok: bool
    score: float | None
    metrics: dict = field(default_factory=dict)
    segments: dict[str, dict] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            lines = [f"INVALID submission ({len(self.errors)} error(s)):"]
            lines += [f"  row {e.get('row')}: {e.get('message')}" for e in self.errors[:20]]
            return "\n".join(lines)
        m = self.metrics
        return (
            f"score {self.score:6.2f} | "
            f"alloc F1 {m['allocation_f1']:.4f} (P {m['precision']:.4f} / R {m['recall']:.4f}) | "
            f"txn acc {m['txn_accuracy']:.4f} | exc macro-F1 {m['exception_macro_f1']:.4f} | "
            f"amount-weighted acc {m['amount_weighted_accuracy']:.4f} | "
            f"{m['rows']:,} rows over {m['txns_covered']:,}/{m['txns_total']:,} lines"
        )


def golden_for(settings: Settings | None = None, phase: str | None = None):
    """The scorer's `Golden` for a phase, plus the phase and the scorer module.

    Shared by `score_file` and `insights`, so a local explanation is computed from the
    exact same labels the leaderboard uses (CLAUDE.md §13: one definition, no drift).
    """
    settings = settings or load_settings()
    data = Phase(settings, phase or settings.phase)
    golden_path = data.dir / "golden_allocations.csv.gz"
    if not golden_path.exists():
        raise FileNotFoundError(
            f"no golden for phase '{data.name}' (only TRAIN and the sample have labels); "
            "use the coverage report instead"
        )
    scorer = _load_scorer()
    golden = scorer.load_golden(
        str(golden_path),
        str(data.dir / "bank_transactions.csv.gz"),
        EXCEPTIONS,
        CATEGORIES,
    )
    return golden, data, scorer


def score_file(path: Path, settings: Settings | None = None, phase: str | None = None) -> ScoreReport:
    golden, _data, scorer = golden_for(settings, phase)
    result = scorer.score_submission(golden, Path(path).read_bytes(), Path(path).name)
    return ScoreReport(
        ok=bool(result.get("ok")),
        score=result.get("score"),
        metrics=result.get("metrics") or {},
        segments=result.get("segments") or {},
        errors=result.get("errors") or [],
    )


# -------------------------------------------------------------------- coverage


@dataclass(slots=True)
class CoverageReport:
    rows: int = 0
    lines: int = 0
    item_rows: int = 0
    no_item_rows: int = 0
    total_lines: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def coverage(self) -> float:
        return self.lines / self.total_lines if self.total_lines else 0.0

    def summary(self) -> str:
        if not self.ok:
            head = [f"INVALID ({len(self.errors)} problem(s)):"]
            return "\n".join(head + [f"  {e}" for e in self.errors[:20]])
        return (
            f"{self.rows:,} rows | {self.item_rows:,} item + {self.no_item_rows:,} category | "
            f"{self.lines:,}/{self.total_lines:,} bank lines covered ({self.coverage:.1%})"
        )


def _rows(path: Path):
    magic = path.read_bytes()[:2]
    if path.suffix == ".gz" or magic == b"\x1f\x8b":
        if magic != b"\x1f\x8b":
            raise ValueError(f"{path.name} ends in .gz but is not gzip")
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as fh:
            yield from csv.reader(fh)
    else:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            yield from csv.reader(fh)


def validate_file(path: Path, settings: Settings | None = None, phase: str | None = None) -> CoverageReport:
    """Structural check a human would do by hand: this is what the server enforces."""
    settings = settings or load_settings()
    data = Phase(settings, phase or settings.phase)
    txns = set(_column(data.dir / "bank_transactions.csv.gz", "bank_txn_id"))
    report = CoverageReport(total_lines=len(txns))

    seen_pairs: set[tuple[str, str]] = set()
    item_lines: set[str] = set()
    no_item_lines: set[str] = set()

    reader = _rows(path)
    header = [h.strip().lower() for h in next(reader, [])]
    if header != SUBMISSION_COLUMNS:
        report.errors.append(f"bad header {header}, expected {SUBMISSION_COLUMNS}")
        return report

    for lineno, cells in enumerate(reader, start=2):
        if not cells or not any(cells):
            continue
        report.rows += 1
        if len(cells) != 5:
            report.errors.append(f"row {lineno}: expected 5 fields, got {len(cells)}")
            continue
        row = tuple(c.strip() for c in cells)
        report.errors.extend(f"row {lineno}: {msg}" for msg in validate_row(row, txns))
        txn, item = row[0], row[1]
        if item:
            if (txn, item) in seen_pairs:
                report.errors.append(f"row {lineno}: duplicate ({txn}, {item})")
            seen_pairs.add((txn, item))
            item_lines.add(txn)
            report.item_rows += 1
        else:
            if txn in no_item_lines:
                report.errors.append(f"row {lineno}: two no-item rows for {txn}")
            no_item_lines.add(txn)
            report.no_item_rows += 1

    for txn in sorted(item_lines & no_item_lines):
        report.errors.append(f"{txn}: has both item rows and a category row")
    report.lines = len(item_lines | no_item_lines)
    return report


def _column(path: Path, column: str) -> list[str]:
    import polars as pl

    return pl.read_csv(path, columns=[column], infer_schema_length=0)[column].to_list()


# ------------------------------------------------------------------- dispatch


def evaluate_build(build: Build, settings: Settings | None = None) -> tuple[ScoreReport | None, CoverageReport]:
    """Score when a golden exists, otherwise report coverage. Never invents a score."""
    settings = settings or load_settings()
    data = Phase(settings, build.phase)
    coverage = validate_file(build.file, settings, build.phase)
    scored = None
    if data.has_golden():
        try:
            scored = score_file(build.file, settings, build.phase)
        except FileNotFoundError:
            scored = None
    return scored, coverage


def metrics_for(build: Build, scored: ScoreReport | None, coverage: CoverageReport) -> dict:
    """What goes into builds/index.json (and therefore into the push warning)."""
    metrics: dict = {
        "rows": coverage.rows,
        "lines": coverage.lines,
        "total_lines": coverage.total_lines,
        "coverage": round(coverage.coverage, 6),
        "item_rows": coverage.item_rows,
        "no_item_rows": coverage.no_item_rows,
        "valid": coverage.ok,
    }
    if scored is not None and scored.ok:
        metrics.update({"score": scored.score, **scored.metrics})
        metrics["segments"] = scored.segments
    elif scored is not None:
        metrics["valid"] = False
        metrics["errors"] = scored.errors
    return metrics


def format_segments(metrics: dict, worst: int = 12) -> str:
    segments = metrics.get("segments") or {}
    if not segments:
        return ""
    rows = sorted(segments.items(), key=lambda kv: kv[1]["accuracy"])
    out = ["lowest-accuracy segments (where to work next):"]
    out += [f"  {name:<28} n={info['n']:>7,}  acc={info['accuracy']:.3f}" for name, info in rows[:worst]]
    return "\n".join(out)


__all__ = [
    "CoverageReport",
    "ScoreReport",
    "evaluate_build",
    "format_segments",
    "golden_for",
    "metrics_for",
    "score_file",
    "validate_file",
    "json",
]
