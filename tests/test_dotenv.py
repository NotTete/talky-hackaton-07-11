"""`.env` is documented as read by the CLI (see .env.example). This keeps that true."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os  # noqa: E402

import pytest  # noqa: E402

from bankrec.config import load_dotenv  # noqa: E402


@pytest.fixture(autouse=True)
def no_leaked_environment():
    """load_dotenv() writes into os.environ, which monkeypatch cannot undo for keys it
    never touched. Snapshot the whole environment so one test cannot set HK_API for the
    rest of the session (it did: a later test then talked to example.test)."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_reads_key_value_and_strips_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "HK_TOKEN=\"hk_from_file\"\n"
        "export BANKREC_PHASE=live\n"
        "\n"
        "HK_API='https://example.test/api'\n"
    )
    for key in ("HK_TOKEN", "HK_API", "BANKREC_PHASE"):
        monkeypatch.delenv(key, raising=False)
    assert load_dotenv(env) == env
    assert os.environ["HK_TOKEN"] == "hk_from_file"
    assert os.environ["BANKREC_PHASE"] == "live"
    assert os.environ["HK_API"] == "https://example.test/api"


def test_the_shell_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HK_TOKEN=from_file\n")
    monkeypatch.setenv("HK_TOKEN", "from_shell")
    load_dotenv(env)
    assert os.environ["HK_TOKEN"] == "from_shell", "an exported token must not be replaced"

    load_dotenv(env, override=True)
    assert os.environ["HK_TOKEN"] == "from_file"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") is None
