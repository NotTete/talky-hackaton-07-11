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


def test_insights_match_the_scorer_and_are_deterministic(sample_settings):
    """The explanation must agree with the official number, and read the same twice."""
    from bankrec.hooks import TraceRecorder
    from bankrec.insights import diagnose, render

    build = process(sample_settings, "sample", name="insight", hooks=[TraceRecorder(sample_settings.builds_dir)])
    assert build.trace, "process --trace should publish a trace sidecar"

    scored = score_file(build.file, sample_settings, "sample")
    assert scored.ok, scored.summary()
    first = diagnose(build, sample_settings, examples=1)
    second = diagnose(build, sample_settings, examples=1)

    # line success is derived independently, so it must agree with the official scorer
    assert abs(first.lines_ok / first.lines - scored.metrics["txn_accuracy"]) < 0.002
    assert first.trace and first.reason_all
    assert render(first) == render(second)


def test_experiment_provenance_and_self_comparison(sample_settings):
    """A run records what produced it, and comparing a build to itself is a no-op."""
    from bankrec.builds import Builds
    from bankrec.experiments import compare_builds, runs_table

    build = process(
        sample_settings,
        "sample",
        name="prov",
        note="provenance check",
        tags=["t"],
        limit=300,
    )
    assert build.note == "provenance check" and build.tags == ["t"]
    assert build.provenance.get("code_sha")
    assert "git" in build.provenance and "data" in build.provenance

    comparison = compare_builds(build, build, sample_settings, examples=0)
    assert comparison.net == 0 and not comparison.fixed and not comparison.broken

    table = runs_table(Builds(sample_settings.builds_dir), "sample")
    assert build.id in table and "provenance check" in table


def test_document_report_is_structural_and_attaches(sample_settings):
    """Preprocessing has its own report: parsed families, formats, and join health."""
    from bankrec.data import Phase
    from bankrec.documents import build_document_report
    from bankrec.preprocess import DocumentContext, load_advices

    phase = Phase(sample_settings, "sample")
    txns = phase.bank_transactions()
    report = build_document_report(phase, DocumentContext(phase), load_advices(phase), txns)

    assert report.families["dd_batch"].groups > 0
    assert report.families["dd_batch"].with_reference > 0
    assert report.formats["csv"].parsed > 0 and report.formats["csv"].entries > 0
    assert report.bank_lines == len(txns) and report.group_lines > 0
    assert any("pdf" in warning for warning in report.warnings)


def test_stage_registry_is_content_addressed(tmp_path):
    """A preprocessing run is versioned by stage + metrics + report hashes + code."""
    from bankrec.config import load_settings
    from bankrec.stages import Stages, record, render_table, stages_root

    settings = load_settings()
    settings.builds_dir = tmp_path / "builds"
    bench = tmp_path / "bench.json"
    bench.write_text('{"accuracy": 0.9}', encoding="utf-8")

    first = record("pdf_layout", "sample", settings=settings, note="bench", metrics={"accuracy": 0.9}, reports=[bench])
    second = record("pdf_layout", "sample", settings=settings, note="bench", metrics={"accuracy": 0.9}, reports=[bench])
    assert first.id == second.id
    assert first.reports and len(first.reports[0]["sha256"]) == 64
    assert first.provenance.get("code_sha")

    runs = Stages(stages_root(settings)).list("pdf_layout", "sample")
    assert [run.id for run in runs] == [first.id]
    assert "pdf_layout" in render_table(runs)
