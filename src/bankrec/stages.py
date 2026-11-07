"""Stage runs: preprocessing experiments that produce a report, not a submission.

`experiments.py` tracks runs that end in a scored CSV. Preprocessing (PDF layout and
language detection, batch/payment-run extraction, advice parsing) does not produce a
submission — it produces *evidence* and a report. Those runs still need the same three
versions (code, data, report) or "the parser got better" cannot be reproduced.

So a stage run is a small record:

    stage      pdf_layout | advice_xlsx | payment_runs | ...
    phase      sample | train | live
    metrics    flat numbers the stage measured (accuracy, docs, flags, ...)
    reports    files it wrote (JSONL/JSON/TXT), stored by sha256
    warnings   what it could not do
    provenance git commit + dirty + code sha + data fingerprint (same as a build)

Register from a script:

    from bankrec.stages import record
    record("pdf_layout", "sample", note="stopwords vs fusion",
           metrics={"accuracy": 0.93, "docs": 1200},
           reports=[Path("experiments/pdf_layout/out/lang_bench.json")])

or from the shell:

    python -m bankrec stage --stage pdf_layout --phase sample \
        --note "fusion + quality gate" --metric accuracy=0.93 --metric docs=1200 \
        --report experiments/pdf_layout/out/lang_bench.json
    python -m bankrec stages

The registry is `stages/index.json` + one sidecar per run, same as builds. No server.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .builds import slugify
from .config import Settings, load_settings
from .experiments import capture_provenance


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _fmt(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def format_metrics(metrics: dict, limit: int = 0) -> str:
    items = list(metrics.items())
    if limit:
        items = items[:limit]
    return " ".join(f"{key}={_fmt(value)}" for key, value in items) or "—"


def _hash_report(path: Path) -> dict:
    data = path.read_bytes()
    return {
        "name": path.name,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _run_id(stage: str, phase: str, payload: dict) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return f"{slugify(stage, 20)}-{slugify(phase, 10)}-{digest}"


@dataclass(slots=True)
class StageRun:
    id: str
    stage: str
    phase: str
    created_at: str
    note: str = ""
    tags: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    reports: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def summary(self, width: int = 52) -> str:
        code = (self.provenance.get("code_sha") or "—")[:8]
        git = self.provenance.get("git") or {}
        if git.get("commit"):
            code = f"{code}{'*' if git.get('dirty') else ''}"
        metrics = format_metrics(self.metrics, limit=4)
        note = self.note[:width]
        return f"{self.id:<40} {self.stage:<14} {self.phase:<7} {code:<10} {metrics:<44} {note}"


class Stages:
    """Index over `stages/*.json` (sidecars), mirroring `builds`."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.index_path = self.root / "index.json"
        self.entries: dict[str, StageRun] = {}
        self._load()

    def _load(self) -> None:
        if self.index_path.exists():
            for key, value in (json.loads(self.index_path.read_text()).get("stages") or {}).items():
                self.entries[key] = StageRun(**value)
        if self.root.is_dir():
            for sidecar in sorted(self.root.glob("*.json")):
                if sidecar.name == "index.json":
                    continue
                try:
                    run = StageRun(**json.loads(sidecar.read_text()))
                except Exception:  # a broken sidecar must not break the registry
                    continue
                self.entries.setdefault(run.id, run)

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now(),
            "stages": {key: asdict(run) for key, run in sorted(self.entries.items())},
        }
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(self.index_path)

    def add(self, run: StageRun) -> StageRun:
        self.entries[run.id] = run
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{run.id}.json").write_text(json.dumps(asdict(run), indent=2, ensure_ascii=False) + "\n")
        self.save()
        return run

    def get(self, run_id: str) -> StageRun | None:
        run = self.entries.get(run_id)
        if run:
            return run
        return self.entries.get(Path(run_id).name.split(".")[0])

    def list(self, stage: str | None = None, phase: str | None = None) -> list[StageRun]:
        runs = [
            run
            for run in self.entries.values()
            if (stage is None or run.stage == stage) and (phase is None or run.phase == phase)
        ]
        position = {key: i for i, key in enumerate(self.entries)}
        return sorted(runs, key=lambda r: (r.created_at, position.get(r.id, -1)), reverse=True)

    def latest(self, stage: str | None = None, phase: str | None = None) -> StageRun | None:
        runs = self.list(stage, phase)
        return runs[0] if runs else None


def stages_root(settings: Settings | None = None) -> Path:
    settings = settings or load_settings()
    return settings.builds_dir.parent / "stages"


def record(
    stage: str,
    phase: str,
    *,
    settings: Settings | None = None,
    note: str = "",
    tags: list[str] | None = None,
    metrics: dict | None = None,
    reports: list[Path] | None = None,
    warnings: list[str] | None = None,
) -> StageRun:
    """Persist one preprocessing run. Deterministic id from its content."""
    settings = settings or load_settings()
    report_entries = [_hash_report(Path(path)) for path in (reports or []) if Path(path).exists()]
    metrics = {str(k): v for k, v in (metrics or {}).items()}
    provenance = capture_provenance(settings, phase)
    payload = {
        "stage": stage,
        "phase": phase,
        "note": note,
        "tags": list(tags or []),
        "metrics": metrics,
        "reports": [(r["name"], r["sha256"]) for r in report_entries],
        "warnings": list(warnings or []),
        # same metrics on different code is a different result, not the same run
        "code_sha": provenance.get("code_sha"),
        "manifest_sha": (provenance.get("data") or {}).get("manifest_sha"),
    }
    run = StageRun(
        id=_run_id(stage, phase, payload),
        stage=stage,
        phase=phase,
        created_at=_now(),
        note=note,
        tags=list(tags or []),
        metrics=metrics,
        reports=report_entries,
        warnings=list(warnings or []),
        provenance=provenance,
    )
    return Stages(stages_root(settings)).add(run)


def render_table(runs: list[StageRun]) -> str:
    if not runs:
        return "no stage runs — record one with `bankrec stage --stage ... --phase ...`"
    header = f"{'run':<40} {'stage':<14} {'phase':<7} {'code':<10} {'metrics':<44} note"
    return "\n".join([header] + [run.summary() for run in runs])


def compare_runs(before: StageRun, after: StageRun) -> str:
    """Metric deltas and report-hash changes between two stage runs of the same stage."""
    out = ["", f"STAGE COMPARISON  {before.id} -> {after.id}", ""]
    keys = sorted(set(before.metrics) | set(after.metrics))
    if keys:
        out.append(f"  {'metric':<24} {'before':>12} {'after':>12}  delta")
        for key in keys:
            old = before.metrics.get(key)
            new = after.metrics.get(key)
            if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                out.append(f"  {key:<24} {_fmt(old):>12} {_fmt(new):>12}  {new - old:+.4g}")
            else:
                out.append(f"  {key:<24} {_fmt(old) if old is not None else '—':>12} {_fmt(new) if new is not None else '—':>12}  —")
    old_reports = {r["name"]: r["sha256"] for r in before.reports}
    new_reports = {r["name"]: r["sha256"] for r in after.reports}
    changed = [name for name in sorted(old_reports.keys() | new_reports.keys()) if old_reports.get(name) != new_reports.get(name)]
    if changed:
        out.append("")
        out.append("  reports")
        for name in changed:
            state = "new" if name not in old_reports else "removed" if name not in new_reports else "changed"
            out.append(f"    {name:<40} {state}")
    if before.provenance.get("code_sha") != after.provenance.get("code_sha"):
        out.append("")
        out.append(
            f"  code {before.provenance.get('code_sha', '—')} -> {after.provenance.get('code_sha', '—')}"
        )
    return "\n".join(out)


__all__ = ["StageRun", "Stages", "compare_runs", "format_metrics", "record", "render_table", "stages_root"]
