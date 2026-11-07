"""Extension points for `process` — observe each decision without changing it.

A hook is called once per bank line *after* the matcher has decided, with the decision
it made (`reason` = which rule fired, how many candidates it saw, what it settled).

Why "observe only" is a rule and not an implementation detail
--------------------------------------------------------------
Matching is a chronological book: an item's open balance after line *n* is what line
*n+1* sees (CLAUDE.md §7). A hook that settled items — or that ran a second pass over
the file — would change that book and silently break `PARTIAL` / `DUPLICATE_PAYMENT` /
`RETURNED`. So hooks receive an immutable `LineDecision` and nothing else. The only
thing they may do is write evidence somewhere for `evaluate --explain` to read.

Turning it on
-------------
    bankrec process --phase sample --name trial --trace
    bankrec evaluate --explain

The trace is a `<build-id>.trace.jsonl.gz` sidecar, written in bank-line order so two
runs over the same input produce the same bytes (`gzip mtime=0`, see CLAUDE.md §12).
"""

from __future__ import annotations

import gzip
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class LineDecision:
    """What the matcher did with one bank line. Immutable from the hook's point of view."""

    bank_txn_id: str
    reason: str
    candidates: int
    items: list[str]
    amounts: list[int]
    exceptions: list[str]
    # Why the line resolved the way it did: reference states, every step tried and its
    # outcome, and the step that finally settled it. Empty unless the engine was probing.
    probe: dict | None = None


@runtime_checkable
class DecisionHook(Protocol):
    """One method per line, one method when the build is published."""

    def on_line(self, decision: LineDecision) -> None: ...

    def finalize(self, builds_root: Path, build_id: str) -> str | None:
        """Persist whatever the hook collected; return the path of the artifact (or None)."""


class TraceRecorder:
    """Buffer decisions as JSONL, then gzip them next to the build.

    Kept separate from the build CSV on purpose: the submission must stay exactly what
    the scorer consumes, and a debug column would be one more thing to keep valid.
    """

    def __init__(self, root: Path | str = "builds") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tmp = self.root / f".trace-{time.time_ns()}.jsonl"
        self._fh = self._tmp.open("w", encoding="utf-8", newline="")

    def on_line(self, decision: LineDecision) -> None:
        self._fh.write(
            json.dumps(
                {
                    "t": decision.bank_txn_id,
                    "r": decision.reason,
                    "c": decision.candidates,
                    "i": decision.items,
                    "a": decision.amounts,
                    "x": decision.exceptions,
                    **({"p": decision.probe} if decision.probe else {}),
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )

    def finalize(self, builds_root: Path, build_id: str) -> str | None:
        if not self._fh.closed:
            self._fh.close()
        final = Path(builds_root) / f"{build_id}.trace.jsonl.gz"
        # mtime=0 so an identical run hashes identically (CLAUDE.md §12)
        final.write_bytes(gzip.compress(self._tmp.read_bytes(), mtime=0))
        self._tmp.unlink(missing_ok=True)
        return str(final)


def trace_path(build) -> Path:
    """The sidecar next to a build (`<build-id>.trace.jsonl.gz`), whether or not it exists."""
    declared = getattr(build, "trace", "")
    if declared:
        return Path(declared)
    stem = Path(build.path).name.split(".")[0]
    return Path(build.path).with_name(f"{stem}.trace.jsonl.gz")


def load_probes(build) -> dict[str, dict]:
    """bank_txn_id -> probe, for builds traced with the engine probing. Empty otherwise."""
    path = trace_path(build)
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                row = json.loads(line)
                if "p" in row:
                    out[row["t"]] = row["p"]
    return out


def load_trace(build) -> dict[str, tuple[str, int]]:
    """bank_txn_id -> (reason, n_candidates). Empty dict when the build has no trace."""
    path = trace_path(build)
    if not path.exists():
        return {}
    out: dict[str, tuple[str, int]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[row["t"]] = (row.get("r") or "none", int(row.get("c") or 0))
    return out


__all__ = ["DecisionHook", "LineDecision", "TraceRecorder", "load_trace", "trace_path"]
