"""Smoke test: the sample phase produces a submission the official scorer accepts.

    make test        (or: PYTHONPATH=src python -m pytest -q)

Skips when the data has not been downloaded yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bankrec.config import load_settings  # noqa: E402
from bankrec.evaluate import score_file  # noqa: E402
from bankrec.pipeline import run  # noqa: E402


@pytest.fixture(scope="module")
def sample_settings():
    settings = load_settings()
    settings.phase = "sample"
    settings.item_phases = ["sample"]
    if not (settings.phase_dir / "bank_transactions.csv.gz").exists():
        pytest.skip("sample data missing: run ./scripts/download_data.sh")
    return settings


def test_sample_submission_scores(sample_settings):
    stats = run(sample_settings, "sample")
    assert stats.output and stats.output.exists()
    assert stats.lines > 0
    # every bank line must be covered by at least one row
    assert stats.item_rows + stats.no_item_rows >= stats.lines

    report = score_file(stats.output, sample_settings, "sample")
    assert report.ok, report.summary()
    assert report.score is not None and report.score > 0
