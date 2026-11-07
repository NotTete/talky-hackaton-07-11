"""Submit a file to the hackathon platform (same flow the site documents).

    read -rs HK_TOKEN && export HK_TOKEN
    python -m bankrec submit LIVE submissions/submission_live.csv.gz
    python -m bankrec submit FINAL submissions/submission_final.csv.gz

Flow: reserve a slot (presigned S3 POST) -> upload -> queue scoring -> poll.
FINAL accepts exactly ONE scored submission per team, so validate on LIVE first.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_API = "https://hackathon.usetalky.com/api"
FINAL_STATES = {"scored", "received", "invalid", "error"}
POLL_SECONDS = 5
POLL_TIMEOUT = 600


def _post_json(url: str, token: str, payload: dict, *, expect: int = 200) -> dict:
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
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != expect:
                raise RuntimeError(f"{url} -> HTTP {resp.status}: {resp.read()[:500]!r}")
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url} -> HTTP {exc.code}: {exc.read()[:500]!r}") from exc


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read() or b"{}")


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


def submit(phase: str, path: Path, api: str = "", token: str = "") -> dict:
    phase = phase.upper()
    if phase not in {"LIVE", "FINAL"}:
        raise ValueError("phase must be LIVE or FINAL")
    api = (api or os.environ.get("HK_API") or DEFAULT_API).rstrip("/")
    token = token or os.environ.get("HK_TOKEN", "")
    if not token:
        raise SystemExit("HK_TOKEN is not set (read -rs HK_TOKEN && export HK_TOKEN)")
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")

    up = _post_json(
        f"{api}/submissions/upload-url",
        token,
        {"phase": phase, "filename": path.name},
        expect=201,
    )
    sub_id = up["sub_id"]
    body, content_type = _multipart(up["upload"]["fields"], path)
    req = urllib.request.Request(
        up["upload"]["url"], data=body, method="POST", headers={"Content-Type": content_type}
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            if resp.status not in (200, 201, 204):
                raise SystemExit(f"S3 upload failed: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"S3 upload failed: HTTP {exc.code} {exc.read()[:500]!r}") from exc

    _post_json(f"{api}/submissions/{sub_id}/score", token, {})

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


__all__ = ["submit", "main", "DEFAULT_API"]
