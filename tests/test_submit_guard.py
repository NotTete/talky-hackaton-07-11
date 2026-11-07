"""The human gate on uploading must hold: no terminal, no submission.

This is the one place where a mistake is invisible to the scorer and expensive in
reality (a FINAL shot is spent once), so it gets a test — see CLAUDE.md §17/§19.

Nothing here touches the network: the no-TTY path aborts before any HTTP call.
"""

from __future__ import annotations

import gzip
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bankrec import submit as submit_mod  # noqa: E402
from bankrec.submit import describe, submit  # noqa: E402


@pytest.fixture
def payload(tmp_path) -> Path:
    path = tmp_path / "submission_live.csv.gz"
    with gzip.open(path, "wt", newline="") as fh:
        fh.write("bank_txn_id,item_id,amount,category,exception\n")
        fh.write("TXN1,INV1,100.00,,\n")
        fh.write("TXN2,,,BANK_FEE,\n")
    return path


def test_describe_counts_decompressed_rows(payload):
    info = describe(payload)
    assert info["gzip"] is True
    assert info["rows"] == 2
    assert len(info["sha256"]) == 64
    # the hash is of the CSV, not the .gz (gzip carries a timestamp: CLAUDE.md §12)
    assert info["sha256"] == __import__("hashlib").sha256(
        gzip.open(payload, "rb").read()
    ).hexdigest()


def test_aborts_without_a_terminal(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc:
        submit_mod._gate("LIVE", describe(payload))
    assert "refusing to submit" in str(exc.value)


def test_aborts_on_wrong_answer(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    with pytest.raises(SystemExit) as exc:
        submit_mod._gate("LIVE", describe(payload))
    assert "aborted" in str(exc.value)


def test_final_needs_the_long_phrase(monkeypatch, payload):
    # typing the phase name is enough for LIVE, not for the single FINAL shot
    monkeypatch.setattr(sys, "stdin", io.StringIO("LIVE\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    with pytest.raises(SystemExit):
        submit_mod._gate("FINAL", describe(payload))


def test_never_calls_the_network_without_confirmation(monkeypatch, payload):
    monkeypatch.setenv("HK_TOKEN", "dummy")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    def boom(*_a, **_k):  # any HTTP at all is a failure here
        raise AssertionError("submit() reached the network without confirmation")

    monkeypatch.setattr(submit_mod, "_post_json", boom)
    monkeypatch.setattr(submit_mod, "_get_json", boom)
    with pytest.raises(SystemExit):
        submit("LIVE", payload)


# ---------------------------------------------------------------------------
# the token, and the two mistakes that produce a 401 instead of an upload
# ---------------------------------------------------------------------------
def test_team_token_is_cleaned(monkeypatch):
    from bankrec.submit import team_token

    monkeypatch.delenv("HK_TOKEN", raising=False)
    assert team_token("  hk_abc123  ") == "hk_abc123"
    assert team_token('"hk_abc123"') == "hk_abc123"
    assert team_token("'hk_abc123'") == "hk_abc123"
    assert team_token("Bearer hk_abc123") == "hk_abc123"
    assert team_token('"Bearer hk_abc123"\n') == "hk_abc123"
    monkeypatch.setenv("HK_TOKEN", " hk_from_env\n")
    assert team_token() == "hk_from_env"


def test_whoami_reports_a_bad_token_without_a_traceback(monkeypatch):
    import urllib.error

    from bankrec import submit as submit_mod

    def unauthorised(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"code":"UNAUTHORIZED"}}')
        )

    monkeypatch.setattr(submit_mod.urllib.request, "urlopen", unauthorised)
    monkeypatch.setattr(submit_mod, "team_token", lambda raw="": "bad-token")
    with pytest.raises(SystemExit) as exc:
        submit_mod.whoami()
    message = str(exc.value)
    assert "rejected the team token" in message
    assert "HTTP 401" in message
    assert "whoami" in message


def test_whoami_only_calls_me(monkeypatch):
    from bankrec import submit as submit_mod

    seen = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(req, timeout=60):
        seen.append((req.get_method(), req.full_url, dict(req.headers)))
        return Response(b'{"team":"demo"}')

    monkeypatch.setattr(submit_mod.urllib.request, "urlopen", fake)
    monkeypatch.setattr(submit_mod, "team_token", lambda raw="": "hk_test")
    assert submit_mod.whoami()["team"] == "demo"
    assert len(seen) == 1, "whoami must not request an upload URL: that consumes a slot"
    method, url, headers = seen[0]
    assert method == "GET" and url.endswith("/api/me")
    assert headers.get("Authorization") == "Bearer hk_test"


def test_cli_offers_no_way_to_skip_the_gate():
    """The bypass flag would make FINAL spendable by a script. Pin its absence."""
    from bankrec.cli import build_parser

    parser = build_parser()
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        if action.dest != "command":
            continue
        for sub in action.choices.values():
            text = sub.format_help()
            assert "--yes" not in text and "--force" not in text, f"{sub.prog} exposes a bypass"


def test_202_accepted_is_success_not_a_crash(monkeypatch, payload):
    """/score answers 202 Accepted with the queued submission. That is not an error."""
    from bankrec import submit as submit_mod

    body = b'{"sub_id":"s_7777ad624096fd64060b","phase":"LIVE","status":"queued"}'

    class Response(io.BytesIO):
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(submit_mod.urllib.request, "urlopen", lambda req, timeout=60: Response(body))
    result = submit_mod._post_json("https://example.test/api/submissions/x/score", "t", {})
    assert result["status"] == "queued"


def test_non_2xx_is_a_clean_message(monkeypatch):
    import urllib.error

    from bankrec import submit as submit_mod

    def boom(req, timeout=60):
        raise urllib.error.HTTPError(
            req.full_url, 409, "Conflict", {}, io.BytesIO(b'{"error":{"message":"no slots left"}}')
        )

    monkeypatch.setattr(submit_mod.urllib.request, "urlopen", boom)
    with pytest.raises(SystemExit) as exc:
        submit_mod._post_json("https://example.test/api/submissions/upload-url", "t", {})
    assert "HTTP 409" in str(exc.value) and "no slots left" in str(exc.value)


def test_result_reads_back_one_submission_or_all(monkeypatch):
    from bankrec import submit as submit_mod

    seen = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake(req, timeout=60):
        seen.append((req.get_method(), req.full_url))
        return Response(b'{"status":"scored","score":21.4}')

    monkeypatch.setattr(submit_mod.urllib.request, "urlopen", fake)
    monkeypatch.setattr(submit_mod, "team_token", lambda raw="": "hk_test")

    assert submit_mod.result("s_7777ad624096fd64060b")["score"] == 21.4
    assert submit_mod.submissions()["status"] == "scored"
    assert seen == [
        ("GET", "https://hackathon.usetalky.com/api/submissions/s_7777ad624096fd64060b"),
        ("GET", "https://hackathon.usetalky.com/api/submissions"),
    ]
