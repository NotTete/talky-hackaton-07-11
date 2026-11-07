"""The build repository: every submission is an immutable, named artifact.

Why this exists
---------------
`process → evaluate → push` are three different decisions with three different risks:

* **process** is cheap and repeatable: it may be wrong, and that is fine.
* **evaluate** is where a wrong build is discovered (TRAIN/sample have labels).
* **push** spends something you cannot get back — a LIVE slot, or the *single* FINAL
  shot. It must be a conscious act on a *specific, known* artifact.

Pointing `push` at "whatever is in submissions/ right now" loses exactly the
information you need at that moment: which build is this, was it evaluated, is it the
newest one, has it already been sent? So builds get an identity:

    builds/20261107-1045-a1b2c3d4-baseline.csv.gz
           │        │     │        └── optional name (slug)
           │        │     └────────── content hash, first 8 hex of sha256
           │        └──────────────── build time (local), HHMM
           └───────────────────────── build date

plus a sidecar `…json` (portable, survives a lost index) and `builds/index.json`
(queryable: phase, score, pushed).

The **hash** identifies the content, the stamp says when you built it: rebuilding the same
CSV gives a new stamp but the *same hash*, so a re-run cannot silently pass itself off as a
different result, and "identical content already pushed" is a fact the registry can prove.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "builds"
SLUG_RE = re.compile(r"[^a-z0-9]+")
BUILD_ID_RE = re.compile(r"^(?P<stamp>\d{8}-\d{4})-(?P<sha>[0-9a-f]{8})(?:-(?P<name>[a-z0-9-]+))?$")


def slugify(name: str, limit: int = 40) -> str:
    slug = SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:limit].strip("-")


@dataclass(slots=True)
class Build:
    id: str
    phase: str
    path: str
    sha256: str
    bytes: int
    rows: int = 0
    lines: int = 0
    item_rows: int = 0
    no_item_rows: int = 0
    seconds: float = 0.0
    created_at: str = ""
    name: str = ""
    item_phases: list[str] = field(default_factory=list)
    #: filled by `evaluate` (only phases with a golden have a score)
    metrics: dict = field(default_factory=dict)
    evaluated_at: str = ""
    #: optional `<build-id>.trace.jsonl.gz` written by a DecisionHook (see hooks.py)
    trace: str = ""
    #: filled by `push`
    pushed: dict = field(default_factory=dict)

    @property
    def file(self) -> Path:
        return Path(self.path)

    @property
    def evaluated(self) -> bool:
        return bool(self.evaluated_at)

    @property
    def score(self) -> float | None:
        value = self.metrics.get("score")
        return float(value) if value is not None else None

    def summary(self) -> str:
        score = f"{self.score:6.2f}" if self.score is not None else "   —  "
        pushed = "pushed" if self.pushed else "      "
        name = f"  {self.name}" if self.name else ""
        return (
            f"{self.id:<34} {self.phase:<6} {score} {pushed} "
            f"{self.rows:>7,} rows / {self.lines:>7,} lines{name}"
        )


class Builds:
    """Index over `builds/*.json`, with the CSV next to it."""

    def __init__(self, root: Path | str = DEFAULT_DIR):
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        self.entries: dict[str, Build] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if self.index_path.exists():
            raw = json.loads(self.index_path.read_text()).get("builds", {})
            for key, value in raw.items():
                self.entries[key] = Build(**value)
        if self.root.is_dir():
            for sidecar in sorted(self.root.glob("*.json")):
                if sidecar.name == "index.json":
                    continue
                try:
                    payload = json.loads(sidecar.read_text())
                    build = Build(**payload)
                except Exception:  # a broken sidecar must not break the workflow
                    continue
                self.entries.setdefault(build.id, build)

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now(),
            "builds": {key: asdict(build) for key, build in sorted(self.entries.items())},
        }
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.index_path)

    # --------------------------------------------------------------- writes
    def register(self, build: Build) -> Build:
        self.entries[build.id] = build
        (self.root / f"{build.id}.json").write_text(
            json.dumps(asdict(build), indent=2, ensure_ascii=False) + "\n"
        )
        self.save()
        return build

    def mark_evaluated(self, build_id: str, metrics: dict) -> Build:
        build = self.entries[build_id]
        build.metrics = metrics
        build.evaluated_at = _now()
        return self.register(build)

    def mark_pushed(self, build_id: str, result: dict) -> Build:
        build = self.entries[build_id]
        build.pushed = {
            "at": _now(),
            "phase": build.phase.upper(),
            "sub_id": result.get("sub_id"),
            "status": result.get("status"),
            "score": result.get("score"),
        }
        return self.register(build)

    # --------------------------------------------------------------- reads
    def get(self, build_id: str) -> Build | None:
        build = self.entries.get(build_id)
        if build:
            return build
        # accept a path or a filename
        stem = Path(build_id).name.split(".")[0]
        return self.entries.get(stem)

    def list(self, phase: str | None = None) -> list[Build]:
        items = [
            b
            for b in self.entries.values()
            if phase is None or b.phase.lower() == phase.lower()
        ]
        # created_at has second resolution: two builds in the same second must still be
        # ordered by when they entered the registry, or "latest" is a coin flip.
        position = {key: i for i, key in enumerate(self.entries)}
        return sorted(items, key=lambda b: (b.created_at, position.get(b.id, -1)), reverse=True)

    def latest(self, phase: str | None = None, *, evaluated_only: bool = False) -> Build | None:
        for build in self.list(phase):
            if evaluated_only and not build.evaluated:
                continue
            if not build.file.exists():
                continue
            return build
        return None

    def last_pushed(self, phase: str) -> Build | None:
        pushed = [b for b in self.list(phase) if b.pushed]
        return pushed[0] if pushed else None  # list() is newest-first


class BuildWriter:
    """Write rows to a temp file, hash the *content*, then name and publish the build."""

    def __init__(self, root: Path | str, phase: str, name: str = "", compress: bool = True):
        self.dir = Path(root)
        self.phase = phase
        self.name = name
        self.compress = compress
        self._tmp = self.dir / f".incoming-{time.time_ns()}.csv"
        self._fh = None
        self._hash = hashlib.sha256()
        self._rows = 0

    def __enter__(self) -> BuildWriter:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = self._tmp.open("w", encoding="utf-8", newline="")
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None and not self._fh.closed:
            self._fh.close()

    def write(self, row) -> None:
        line = _csv_line(row)
        self._fh.write(line)
        self._hash.update(line.encode("utf-8"))
        self._rows += 1

    # ------------------------------------------------------------- finalize
    def publish(self, *, item_phases: list[str] | None = None) -> Build:
        if self._fh is not None:
            self._fh.close()
        digest = self._hash.hexdigest()
        stamp = time.strftime("%Y%m%d-%H%M")
        suffix_name = f"-{slugify(self.name)}" if self.name else ""
        build_id = f"{stamp}-{digest[:8]}{suffix_name}"

        suffix = ".csv.gz" if self.compress else ".csv"
        final = self.dir / f"{build_id}{suffix}"
        if self.compress:
            # mtime=0 makes the gzip bytes reproducible for identical content
            final.write_bytes(gzip.compress(self._tmp.read_bytes(), mtime=0))
            self._tmp.unlink(missing_ok=True)
        else:
            self._tmp.replace(final)

        return Build(
            id=build_id,
            phase=self.phase,
            path=str(final),
            sha256=digest,
            bytes=final.stat().st_size,
            rows=self._rows,
            created_at=_now(),
            name=slugify(self.name),
            item_phases=item_phases or [],
        )


def _csv_line(row) -> str:
    import csv
    import io

    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(row)
    return buffer.getvalue()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


__all__ = ["Build", "Builds", "BuildWriter", "DEFAULT_DIR", "slugify", "BUILD_ID_RE"]
