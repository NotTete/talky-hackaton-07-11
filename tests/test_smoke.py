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
from bankrec.pipeline import process  # noqa: E402


@pytest.fixture(scope="module")
def sample_settings(tmp_path_factory):
    settings = load_settings()
    settings.phase = "sample"
    settings.item_phases = ["sample"]
    # never pollute the real build repository from a test
    settings.builds_dir = tmp_path_factory.mktemp("builds")
    if not (settings.phase_dir / "bank_transactions.csv.gz").exists():
        pytest.skip("sample data missing: run make data")
    return settings


def test_sample_build_is_named_and_scores(sample_settings):
    build = process(sample_settings, "sample", name="smoke")
    assert build.file.exists()
    # date + hash + optional name, e.g. 20261107-1045-a1b2c3d4-smoke
    assert build.id.endswith("-smoke")
    assert build.id.split("-")[1].isdigit() and len(build.sha256) == 64
    assert build.lines > 0
    assert build.item_rows + build.no_item_rows >= build.lines

    report = score_file(build.file, sample_settings, "sample")
    assert report.ok, report.summary()
    assert report.score is not None and report.score > 0
