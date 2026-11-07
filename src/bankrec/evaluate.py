"""Score a submission locally with the official scorer (vendor/bankrec_score.py).

TRAIN and the 1-month sample have a golden, so this is the fastest feedback loop:

    python -m bankrec score submissions/submission_sample.csv.gz
    python -m bankrec score submissions/submission_train.csv.gz --phase train

The numbers are exactly what the leaderboard will show (same code, same weights).
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Settings, load_settings
from .data import Phase
from .schema import CATEGORIES, EXCEPTIONS

SCORER_PATH = ROOT / "vendor" / "bankrec_score.py"


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
    metrics: dict
    segments: dict[str, dict]
    errors: list[dict]

    def summary(self) -> str:
        if not self.ok:
            lines = [f"INVALID submission ({len(self.errors)} error(s)):"]
            for err in self.errors[:20]:
                lines.append(f"  row {err.get('row')}: {err.get('message')}")
            return "\n".join(lines)
        m = self.metrics
        return (
            f"score {self.score:6.2f} | "
            f"alloc F1 {m['allocation_f1']:.4f} (P {m['precision']:.4f} / R {m['recall']:.4f}) | "
            f"txn acc {m['txn_accuracy']:.4f} | exc macro-F1 {m['exception_macro_f1']:.4f} | "
            f"amount-weighted acc {m['amount_weighted_accuracy']:.4f} | "
            f"{m['rows']:,} rows over {m['txns_covered']:,}/{m['txns_total']:,} lines"
        )


def score_file(path: Path, settings: Settings | None = None, phase: str | None = None) -> ScoreReport:
    settings = settings or load_settings()
    data = Phase(settings, phase or settings.phase)
    golden_path = data.dir / "golden_allocations.csv.gz"
    if not golden_path.exists():
        raise FileNotFoundError(
            f"no golden for phase '{data.name}' (only TRAIN and the sample have labels); "
            "validate LIVE/FINAL and submit instead"
        )
    scorer = _load_scorer()
    golden = scorer.load_golden(
        str(golden_path),
        str(data.dir / "bank_transactions.csv.gz"),
        EXCEPTIONS,
        CATEGORIES,
    )
    result = scorer.score_submission(golden, Path(path).read_bytes(), Path(path).name)
    return ScoreReport(
        ok=bool(result.get("ok")),
        score=result.get("score"),
        metrics=result.get("metrics") or {},
        segments=result.get("segments") or {},
        errors=result.get("errors") or [],
    )


def format_segments(report: ScoreReport, worst: int = 12) -> str:
    if not report.ok or not report.segments:
        return ""
    rows = sorted(report.segments.items(), key=lambda kv: kv[1]["accuracy"])
    out = ["lowest-accuracy segments (where to work next):"]
    for name, info in rows[:worst]:
        out.append(f"  {name:<28} n={info['n']:>7,}  acc={info['accuracy']:.3f}")
    return "\n".join(out)


__all__ = ["ScoreReport", "score_file", "format_segments", "json"]
