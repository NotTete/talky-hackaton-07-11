"""The pipeline map must not describe a pipeline that no longer exists.

Cheap, data-free invariants:
  * every symbol a stage claims to implement still imports,
  * rendering is deterministic (same facts in, same bytes out),
  * the generated page has no em dash (house style) and every stage is anchorable.

The numbers themselves are not asserted here: they come from the current submission
and are meant to move. `make map-check` is the guard that the committed file matches
them, and it belongs in the workflow right after a scoring change, not in CI.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_map  # noqa: E402
from build_map import STAGES, Facts, render, verify_stages  # noqa: E402


def test_every_stage_symbol_exists():
    verify_stages()  # raises SystemExit listing the missing symbols


def test_render_is_deterministic():
    facts = Facts(phase="sample")
    theme = build_map.THEME.read_text(encoding="utf-8")
    assert render(facts, theme) == render(facts, theme)


def test_page_is_wellformed_enough():
    theme = build_map.THEME.read_text(encoding="utf-8")
    page = render(Facts(phase="sample"), theme)

    assert not re.search(r"[\u2013\u2014]", page), "em dash and en dash are banned in this repo"
    assert page.startswith("<!doctype html>")
    assert page.count("<main") == 1 and page.count("</main>") == 1

    ids = set(re.findall(r'id="([^"]+)"', page))
    for stage in STAGES:
        assert stage["id"] in ids, f"stage {stage['id']} has no anchor target"
        assert f'href="#{stage["id"]}"' in page


def test_numbers_absent_means_no_fake_numbers():
    # With no submission, the page must say so instead of printing zeros as measurements.
    page = render(Facts(phase="sample", error="no submission"), build_map.THEME.read_text())
    assert "no submission" in page
    assert "score 0.00" not in page
