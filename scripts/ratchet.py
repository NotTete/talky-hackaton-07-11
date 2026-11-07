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


CACHE = ROOT / ".cache" / "ratchet.json"


def _inputs_key() -> str:
    """Everything a sample build depends on: the code, the settings, the sample data.

    A build is a pure function of these (CLAUDE.md §12), so when the key is unchanged the
    last measurement is still true and the 60 s build can be skipped.
    """
    digest = hashlib.sha256()
    files = sorted((ROOT / "src").rglob("*.py")) + [ROOT / "config" / "settings.toml"]
    files += sorted(p for p in (ROOT / "data" / "sample").rglob("*") if p.is_file())
    files += sorted((ROOT / "data" / "masters").rglob("*")) if (ROOT / "data" / "masters").exists() else []
    for path in files:
        if not path.is_file():
            continue
        stat = path.stat()
        if path.suffix == ".py" or path.name == "settings.toml":
            digest.update(path.read_bytes())  # content, not mtime: edits and reverts are exact
        else:
            digest.update(f"{path}:{stat.st_size}:{int(stat.st_mtime)}".encode())
    return digest.hexdigest()


def run(use_cache: bool = True) -> dict:
    """Measure the sample build, reusing the last result when no input changed."""
    key = _inputs_key()
    if use_cache and CACHE.exists():
        cached = json.loads(CACHE.read_text())
        if cached.get("key") == key:
            print("ratchet: inputs unchanged, using the cached measurement (--fresh to rebuild)")
            return cached["result"]
    result = _measure()
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"key": key, "result": result}))
    return result


def _measure() -> dict:
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
    parser.add_argument("--fresh", action="store_true", help="ignore the cache and rebuild")
    args = parser.parse_args()
    now = run(use_cache=not (args.fresh or args.update))
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
