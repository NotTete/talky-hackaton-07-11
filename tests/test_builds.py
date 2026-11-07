"""The build repository: naming, identity, registry, and the warnings before a push.

These are the cheap invariants that make `push` safe to automate up to the human gate:

  * a build id is `YYYYMMDD-HHMM-<content-hash>-<name>` and the hash is the content,
  * the registry survives a round trip and does not invent scores,
  * every condition that should make a human stop is actually reported.

No data and no network: everything here is built from a two-row CSV.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bankrec.builds import BUILD_ID_RE, Builds, BuildWriter, slugify  # noqa: E402
from bankrec.cli import _push_warnings  # noqa: E402

HEADER = ["bank_txn_id", "item_id", "amount", "category", "exception"]
ROWS = [["BT-2025-0001234", "ARI-2025-0456789", "1210.00", "", ""]]


def make_build(root: Path, *, name: str = "", rows=ROWS):
    writer = BuildWriter(root, "sample", name)
    with writer as out:
        out.write(HEADER)
        for row in rows:
            out.write(row)
    build = writer.publish(item_phases=["sample"])
    Builds(root).register(build)
    return build


def test_id_is_date_time_hash_and_optional_name(tmp_path):
    build = make_build(tmp_path, name="clean baseline")
    match = BUILD_ID_RE.match(build.id)
    assert match, build.id
    assert match["stamp"][:8].isdigit() and match["stamp"][9:].isdigit()
    assert match["sha"] == build.sha256[:8]
    assert match["name"] == "clean-baseline" == build.name
    # slug is what goes into the filename
    assert slugify("Fix  PARTIAL / discounts") == "fix-partial-discounts"


def test_identity_is_content_not_clock(tmp_path):
    first = make_build(tmp_path)
    second = make_build(tmp_path)  # same rows, later in the same minute
    assert first.sha256 == second.sha256
    assert first.id.split("-")[2] == second.id.split("-")[2]  # same content hash
    assert first.file.read_bytes() == second.file.read_bytes()  # reproducible gzip


def test_registry_round_trip(tmp_path):
    build = make_build(tmp_path, name="baseline")
    repo = Builds(tmp_path)
    assert repo.get(build.id) is not None
    assert repo.latest("sample").id == build.id
    assert repo.latest("live") is None
    # sidecars survive losing the index
    (tmp_path / "index.json").unlink()
    assert Builds(tmp_path).get(build.id).sha256 == build.sha256


def test_evaluate_never_invents_a_score(tmp_path):
    build = make_build(tmp_path)
    repo = Builds(tmp_path)
    assert build.score is None and not build.evaluated
    repo.mark_evaluated(build.id, {"valid": True, "score": 21.23})
    assert Builds(tmp_path).get(build.id).score == 21.23


def test_warnings_cover_every_reason_to_stop(tmp_path):
    older = make_build(tmp_path, name="older")
    repo = Builds(tmp_path)
    repo.mark_evaluated(older.id, {"valid": True, "score": 20.0})

    newer = make_build(tmp_path, name="newer", rows=[["BT-2025-0001235", "", "", "BANK_FEES", ""]])
    repo = Builds(tmp_path)

    warnings = "\n".join(_push_warnings(repo.get(older.id), repo, "sample"))
    assert "never evaluated" not in warnings  # it was evaluated
    assert "newer build exists" in warnings  # a later build for the same phase

    # unevaluated build
    assert "never evaluated" in "\n".join(_push_warnings(repo.get(newer.id), repo, "sample"))

    # identical content already pushed
    repo.mark_evaluated(newer.id, {"valid": True})
    repo.mark_pushed(newer.id, {"sub_id": "x", "status": "scored", "score": 1.0})
    pushed = Builds(tmp_path)
    assert "already pushed" in "\n".join(_push_warnings(pushed.get(newer.id), pushed, "sample"))

    # FINAL is one shot, and this build was made for another phase
    final_warn = "\n".join(_push_warnings(pushed.get(newer.id), pushed, "final"))
    assert "ONE scored submission" in final_warn
    assert "made for 'sample'" in final_warn


@pytest.mark.parametrize("phase", ["LIVE", "FINAL"])
def test_push_needs_a_build_for_that_phase(tmp_path, phase):
    repo = Builds(tmp_path)
    assert repo.latest(phase.lower()) is None
