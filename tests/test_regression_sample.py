"""Regression net: the sample output must not move unless a rule meant it to.

Slow (one full sample run). Skips when the data is missing. Accept an intended change with
`make ratchet-update` in the same commit as the rule, and say what moved in the message.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ratchet", ROOT / "scripts" / "ratchet.py")
ratchet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ratchet)


@pytest.fixture(scope="module")
def measured():
    if not (ROOT / "data" / "sample" / "golden_allocations.csv.gz").exists():
        pytest.skip("sample data missing: run make data")
    return ratchet.run()


def test_output_hash_and_score_floor(measured):
    floor = json.loads((ratchet.FIXTURES / "sample_floor.json").read_text())
    sha = (ratchet.FIXTURES / "sample.sha256").read_text().strip()
    found = ratchet.problems(measured, floor, sha)
    assert not found, "; ".join(found)
