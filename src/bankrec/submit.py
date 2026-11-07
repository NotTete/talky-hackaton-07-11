"""Submit a file to the hackathon platform (same flow the site documents).

    read -rs HK_TOKEN && export HK_TOKEN
    python -m bankrec push LIVE            # latest evaluated build
    python -m bankrec push FINAL <build-id>

Flow: reserve a slot (presigned S3 POST) -> upload -> queue scoring -> poll.
FINAL accepts exactly ONE scored submission per team, so validate on LIVE first.

**The human gate is not optional.** Uploading cannot be undone (and a FINAL shot is
spent for good), so every submission first prints what it is about to spend — phase,
file, row count, decompressed sha256, coverage — and then waits for the operator to
type `LIVE`, or `SUBMIT FINAL`. There is deliberately **no `--yes` flag and no env
var** that skips it: an automated caller (agent, CI, cron) that cannot show the
summary to a person on a real terminal must not submit. It aborts instead.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from pathlib import Path

from .config import load_dotenv

load_dotenv()  # so `cp .env.example .env` works, as the example file claims

DEFAULT_API = "https://hackathon.usetalky.com/api"
FINAL_STATES = {"scored", "received", "invalid", "error"}
POLL_SECONDS = 5
POLL_TIMEOUT = 600

# The phrase the operator has to type. FINAL is a single shot, so it costs more words.
CONFIRM_PHRASE = {"LIVE": "LIVE", "FINAL": "SUBMIT FINAL"}

# Limits from docs/FORMAT.md, checked before anything is uploaded.
MAX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
MAX_ROWS = 3_000_000


def _api(api: str = "") -> str:
    return (api or os.environ.get("HK_API") or DEFAULT_API).rstrip("/")


def team_token(raw: str = "") -> str:
    """The team token, cleaned.

    Pasted tokens arrive with a trailing newline from a terminal copy, with the quotes a
    shell or an .env file kept, or with the `Bearer ` prefix the docs put in front of the
    header. All three make the header wrong in a way the server reports as a plain 401.
    """
    value = (raw or os.environ.get("HK_TOKEN", "")).strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _auth_error(url: str, code: int, body: bytes, token: str) -> SystemExit:
    """A 401 is always about the token, so say that instead of raising a traceback."""
    try:
        detail = json.loads(body or b"{}").get("error", {}).get("message", "")
    except ValueError:
        detail = (body or b"")[:200].decode("utf-8", "replace")
    fingerprint = f"{len(token)} characters, starts {token[:3]}…" if token else "empty"
    return SystemExit(
        f"the platform rejected the team token (HTTP {code} on {url}).\n"
        f"  token       HK_TOKEN · {fingerprint}\n"
        f"  server      {detail or '(no message)'}\n"
        "\n"
        "The token is issued once, when the team registers, and there is no self-service\n"
        "reset: ask the organisers to reissue it if it is lost. Quotes and a leading\n"
        "'Bearer ' are stripped automatically, so this is a different string from the one\n"
        "the site showed you.\n"
        "\n"
        "Check it without spending an upload slot:  python -m bankrec whoami"
    )


def whoami(api: str = "", token: str = "") -> dict:
    """GET /api/me with the team token. The cheapest way to tell a bad token from bad code.

    It is intentionally the only endpoint the CLI calls before an upload: requesting an
    upload URL already consumes a LIVE slot, `/me` does not.
    """
    token = team_token(token)
    if not token:
        raise SystemExit(
            "HK_TOKEN is not set.\n"
            "  export it:      read -rs HK_TOKEN && export HK_TOKEN\n"
            "  or put it in:   .env  (HK_TOKEN=…, never committed)"
        )
    return _get_json(f"{_api(api)}/me", token)


def submissions(api: str = "", token: str = "") -> dict:
    """GET /api/submissions: everything this team has sent, with status and score.

    The reason this exists: an upload can succeed and the client can still die on its way
    to the score (a 202, a dropped connection, a closed laptop). The submission is in the
    platform's hands at that point, so reading it back is the only way to learn the result.
    """
    token = team_token(token)
    if not token:
        raise SystemExit("HK_TOKEN is not set (read -rs HK_TOKEN && export HK_TOKEN)")
    return _get_json(f"{_api(api)}/submissions", token)


def result(sub_id: str, api: str = "", token: str = "") -> dict:
    """GET /api/submissions/{id}: the status and score of one submission."""
    token = team_token(token)
    if not token:
        raise SystemExit("HK_TOKEN is not set (read -rs HK_TOKEN && export HK_TOKEN)")
    return _get_json(f"{_api(api)}/submissions/{sub_id}", token)


def _body_message(raw: bytes) -> str:
    """The server's error message, if it sent one as JSON."""
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return (raw or b"")[:300].decode("utf-8", "replace")
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    return str(payload)[:300]


def _api_error(url: str, code: int, raw: bytes, token: str) -> SystemExit:
    if code in (401, 403):
        return _auth_error(url, code, raw, token)
    return SystemExit(f"{url} -> HTTP {code}: {_body_message(raw)}")


def _open(req: urllib.request.Request, token: str, timeout: int = 60) -> bytes:
    """One request. Any 2xx is success (the platform answers /score with 202 Accepted).

    Treating 202 as an error is how a live submission gets uploaded, queued and then
    reported as a crash: the site's own client checks `response.ok`, so this does too.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if not 200 <= resp.status < 300:
                raise _api_error(req.full_url, resp.status, data, token)
            return data
    except urllib.error.HTTPError as exc:
        raise _api_error(req.full_url, exc.code, exc.read(), token) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{req.full_url} -> unreachable: {exc.reason}") from exc


def _post_json(url: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    return _as_json(_open(req, token))


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    return _as_json(_open(req, token))


def _as_json(raw: bytes) -> dict:
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    """Encode an S3 presigned POST (fields first, file last, as AWS requires)."""
    boundary = f"----bankrec{uuid.uuid4().hex}"
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    out += (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{file_path.name}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    out += file_path.read_bytes()
    out += f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def describe(path: Path) -> dict:
    """Hash and count the *decompressed* payload, streaming (no 60 MB in RAM).

    Decompressed, because gzip embeds a timestamp: two byte-identical CSV files have
    different .gz hashes (CLAUDE.md §12), so a gz hash cannot be compared across runs
    and is useless as the thing the human is asked to recognise.
    """
    digest = hashlib.sha256()
    uncompressed = 0
    newlines = 0
    tail = b""
    with path.open("rb") as fh:
        is_gzip = fh.read(2) == b"\x1f\x8b"
        fh.seek(0)
        gunzip = zlib.decompressobj(31) if is_gzip else None
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            data = gunzip.decompress(chunk) if gunzip else chunk
            if not data:
                continue
            digest.update(data)
            uncompressed += len(data)
            newlines += data.count(b"\n")
            tail = data[-1:]
    if gunzip is not None:
        rest = gunzip.flush()
        digest.update(rest)
        uncompressed += len(rest)
        newlines += rest.count(b"\n")
        if rest:
            tail = rest[-1:]

    total_lines = newlines + (0 if tail == b"\n" else 1)
    return {
        "path": path,
        "gzip": is_gzip,
        "packed_bytes": path.stat().st_size,
        "uncompressed_bytes": uncompressed,
        "rows": max(total_lines - 1, 0),  # minus the header
        "sha256": digest.hexdigest(),
    }


def _gate(phase: str, info: dict) -> None:
    """Print what is about to be spent, then require a typed confirmation on a TTY."""
    phrase = CONFIRM_PHRASE[phase]
    rows = info["rows"]
    size_mb = info["uncompressed_bytes"] / (1024 * 1024)
    where = "public leaderboard, repeatable" if phase == "LIVE" else "ONE scored submission, final"
    lines = [
        "",
        f"-- about to spend a {phase} submission " + "-" * 30,
        f"  phase            {phase}  ({where})",
        f"  file             {info['path']}",
        f"  rows             {rows:,}",
        f"  uncompressed     {size_mb:.1f} MB  (packed {info['packed_bytes'] / 1024 / 1024:.1f} MB)",
        f"  sha256 (csv)     {info['sha256']}",
    ]
    if rows > MAX_ROWS:
        lines.append(f"  !! {rows:,} rows > {MAX_ROWS:,} limit — the server will reject this")
    if info["uncompressed_bytes"] > MAX_UNCOMPRESSED_BYTES:
        lines.append(f"  !! {size_mb:.1f} MB > 60 MB limit — the server will reject this")
    lines += [
        "  (validate it first: python -m bankrec validate <file> --phase <phase>)",
        "",
        f"Type {phrase!r} to upload, anything else aborts: ",
    ]
    sys.stderr.write("\n".join(lines))
    sys.stderr.flush()

    # No TTY means no person can be shown the summary -> no submission. Ever.
    if not sys.stdin.isatty():
        raise SystemExit(
            "refusing to submit: no terminal to confirm on.\n"
            "A human has to run this command themselves and type "
            f"{phrase!r}. An agent/CI must stop here and hand this over."
        )
    try:
        answer = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):  # Ctrl-C / Ctrl-D == abort
        answer = ""
    if answer.strip() != phrase:
        raise SystemExit("aborted — nothing was uploaded")


def submit(
    phase: str,
    path: Path,
    api: str = "",
    token: str = "",
    *,
    assume_yes: bool = False,
) -> dict:
    """Upload `path` to `phase` and wait for the score.

    `assume_yes` exists for tests only; the CLI never exposes it, so the interactive
    build/validate/submit path always goes through `_gate`.
    """
    phase = phase.upper()
    if phase not in {"LIVE", "FINAL"}:
        raise ValueError("phase must be LIVE or FINAL")
    api = (api or os.environ.get("HK_API") or DEFAULT_API).rstrip("/")
    token = team_token(token)
    if not token:
        raise SystemExit(
            "HK_TOKEN is not set.\n"
            "  export it:      read -rs HK_TOKEN && export HK_TOKEN\n"
            "  or put it in:   .env  (HK_TOKEN=…, never committed)"
        )
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")

    info = describe(path)
    if not assume_yes:
        _gate(phase, info)

    up = _post_json(f"{api}/submissions/upload-url", token, {"phase": phase, "filename": path.name})
    sub_id = up["sub_id"]
    body, content_type = _multipart(up["upload"]["fields"], path)
    req = urllib.request.Request(
        up["upload"]["url"], data=body, method="POST", headers={"Content-Type": content_type}
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            if not 200 <= resp.status < 300:
                raise SystemExit(f"S3 upload failed: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"S3 upload failed: HTTP {exc.code}: "
            + (exc.read()[:400].decode("utf-8", "replace"))
        ) from exc

    _post_json(f"{api}/submissions/{sub_id}/score", token, {})  # 202 Accepted: queued

    deadline = time.time() + POLL_TIMEOUT
    sub = {"sub_id": sub_id, "status": "queued"}
    while sub.get("status") not in FINAL_STATES and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        sub = _get_json(f"{api}/submissions/{sub_id}", token)
        print(f"  status={sub.get('status')} score={sub.get('score')}", file=sys.stderr)
    return sub


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        raise SystemExit("usage: python -m bankrec submit LIVE|FINAL submission.csv[.gz]")
    result = submit(argv[0], Path(argv[1]))
    print(json.dumps(result, indent=2, ensure_ascii=False))


__all__ = [
    "submit", "main", "describe", "whoami", "submissions", "result",
    "team_token", "DEFAULT_API", "CONFIRM_PHRASE",
]
