"""Regression ratchet for the sample phase.

    python scripts/ratchet.py            # check: fails if the output moved or the score dropped
    python scripts/ratchet.py --update   # accept the current build as the new floor

Two fixtures live in tests/fixtures/:

* sample.sha256      sha256 of the *decompressed* submission (gzip embeds a timestamp,
                     CLAUDE.md §12). A pure refactor must leave it byte-identical.
* sample_floor.json  overall score and per-segment accuracy. A rule that is meant to move
                     the output lowers nothing: it raises the floor in the same commit.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bankrec.config import load_settings  # noqa: E402
from bankrec.evaluate import evaluate_build, metrics_for  # noqa: E402
from bankrec.pipeline import process  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SEGMENT_TOLERANCE = 0.002  # accuracy points a segment may lose before the ratchet fails


def run() -> dict:
    """Build the sample with default settings in a scratch repository and measure it."""
    settings = load_settings()
    settings.phase = "sample"
    settings.item_phases = ["sample"]
    with tempfile.TemporaryDirectory() as scratch:
        settings.builds_dir = Path(scratch)
        build = process(settings, "sample", name="ratchet")
        digest = hashlib.sha256(gzip.open(build.file, "rb").read()).hexdigest()
        scored, coverage = evaluate_build(build, settings)
        metrics = metrics_for(build, scored, coverage)
    return {
        "sha256": digest,
        "score": metrics["score"],
        "segments": {k: v["accuracy"] for k, v in metrics["segments"].items()},
    }


def problems(now: dict, floor: dict, sha: str) -> list[str]:
    out = []
    if now["score"] < floor["score"] - 1e-9:
        out.append(f"score {now['score']:.4f} < floor {floor['score']:.4f}")
    for name, acc in floor["segments"].items():
        got = now["segments"].get(name)
        if got is None or got < acc - SEGMENT_TOLERANCE:
            out.append(f"segment {name}: {got} < floor {acc}")
    if now["sha256"] != sha:
        out.append("output hash changed (fine for a rule change: --update; a bug for a refactor)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    now = run()
    FIXTURES.mkdir(exist_ok=True)
    if args.update:
        (FIXTURES / "sample.sha256").write_text(now["sha256"] + "\n")
        (FIXTURES / "sample_floor.json").write_text(
            json.dumps({"score": now["score"], "segments": now["segments"]}, indent=2, sort_keys=True) + "\n"
        )
        print(f"floor updated: score {now['score']:.4f}, {len(now['segments'])} segments")
        return 0
    floor = json.loads((FIXTURES / "sample_floor.json").read_text())
    sha = (FIXTURES / "sample.sha256").read_text().strip()
    found = problems(now, floor, sha)
    print(f"score {now['score']:.4f} (floor {floor['score']:.4f})")
    for line in found:
        print("  FAIL", line)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
