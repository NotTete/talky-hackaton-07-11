"""Experiments without a tracking server: every build carries its own provenance.

Why not MLflow
--------------
MLflow exists to answer "which run produced this number?" when runs are cheap,
distributed and thrown away. Here the opposite is true:

  * the artifact is already **content-addressed** (`builds/<stamp>-<sha8>-<name>`),
    so two identical submissions are the same object and a rebuild proves it;
  * the score is produced by a **vendored scorer** from a fixed golden, not by a
    training loop, so there is nothing to log in-flight;
  * the team is one machine, one DB-less registry, and `builds/index.json` is already
    the queryable list of runs.

What was missing is *provenance* — the ability to say "this +3.61 came from these
exact source files, this config and this data snapshot" — and a *diff* between two
runs that names which lines moved. That is what this module adds:

  capture_provenance()   git commit + dirty flag + code hash + data fingerprint
  compare_builds(a, b)   fixed/broken lines, per-segment and per-failure deltas
  runs_table()           the experiment list, with delta vs a pinned baseline

Everything is plain files next to the build, so it survives `git clean`, travels in a
zip, and never needs a server. If the team later outgrows this, the sidecars are the
export format MLflow would consume.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .builds import Build, Builds
from .config import ROOT, Settings
from .evaluate import golden_for
from .insights import _iter_rows, _load_text, _text_of, classify_line, parse_rows


# ----------------------------------------------------------------- provenance


def _git(*args: str) -> str:
    # The devenv venv exports LD_LIBRARY_PATH pointing at a manylinux libc; inheriting it
    # makes the child `git` fail with a GLIBC_PRIVATE symbol error. Scrub it.
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def git_state() -> dict:
    """Commit + dirty flag. Empty (not fatal) when not a git checkout."""
    commit = _git("rev-parse", "--short", "HEAD")
    status = _git("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status) if commit else None}


def code_fingerprint(root: Path = ROOT) -> str:
    """Hash of the files whose change can change a decision, including uncommitted edits.

    The git commit alone is not enough: during an experiment the tree is usually dirty,
    and "same commit, different files" must not look like the same experiment.
    """
    paths = sorted((root / "src").rglob("*.py"))
    for extra in ("Makefile", "pyproject.toml"):
        candidate = root / extra
        if candidate.exists():
            paths.append(candidate)
    config = root / "config"
    if config.is_dir():
        paths.extend(sorted(config.glob("*.toml")))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def data_fingerprint(settings: Settings, phase: str) -> dict:
    """Cheap snapshot id: manifest hash + the phase's top-level file names and sizes.

    Hashing 1 GB of dumps on every run would make the experiment loop slower than the
    experiment. Size + manifest sha is enough to notice "the data snapshot changed".
    """
    manifest = settings.data_dir / "manifest.json"
    manifest_sha = (
        hashlib.sha256(manifest.read_bytes()).hexdigest()[:16] if manifest.exists() else ""
    )
    files: dict[str, int] = {}
    directory = settings.data_dir / phase
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file():
                files[path.name] = path.stat().st_size
    return {"manifest_sha": manifest_sha, "files": files}


def capture_provenance(settings: Settings, phase: str) -> dict:
    return {
        "run_version": 1,
        "version": __version__,
        "git": git_state(),
        "code_sha": code_fingerprint(),
        "data": data_fingerprint(settings, phase),
    }


# ------------------------------------------------------------------ submission


def read_submission(path: Path) -> dict[str, list[tuple[str, int, str, str]]]:
    """bank_txn_id -> [(item_id, cents, category, exception)] in file order."""
    out: dict[str, list[tuple[str, int, str, str]]] = {}
    reader = _iter_rows(Path(path))
    header = [h.strip().lower() for h in next(reader, [])]
    if not header:
        return out
    ix = {name: i for i, name in enumerate(header)}

    def cents(cell: str) -> int:
        try:
            return int(round(float(cell) * 100))
        except (TypeError, ValueError):
            return 0

    for cells in reader:
        if not cells or not any(cells):
            continue
        try:
            txn = cells[ix["bank_txn_id"]].strip()
            item = cells[ix["item_id"]].strip()
            amount = cents(cells[ix["amount"]]) if cells[ix["amount"]].strip() else 0
            category = cells[ix["category"]].strip()
            exception = cells[ix["exception"]].strip()
        except (KeyError, IndexError):
            continue
        out.setdefault(txn, []).append((item, amount, category, exception))
    return out


# -------------------------------------------------------------------- compare


@dataclass(slots=True)
class LineChange:
    txn: str
    segment: str
    expected: str
    before: str
    after: str
    text: str
    kinds_before: tuple[str, ...] = ()
    kinds_after: tuple[str, ...] = ()


@dataclass(slots=True)
class Comparison:
    a: Build
    b: Build
    metrics_a: dict = field(default_factory=dict)
    metrics_b: dict = field(default_factory=dict)
    lines: int = 0
    fixed: list[LineChange] = field(default_factory=list)
    broken: list[LineChange] = field(default_factory=list)
    fixed_total: int = 0
    broken_total: int = 0
    still_wrong: int = 0
    still_right: int = 0
    kinds_a: Counter = field(default_factory=Counter)
    kinds_b: Counter = field(default_factory=Counter)
    segments: dict[str, dict] = field(default_factory=dict)
    pairs_a: Counter = field(default_factory=Counter)
    pairs_b: Counter = field(default_factory=Counter)

    @property
    def net(self) -> int:
        return self.fixed_total - self.broken_total


def _expected(gitems: dict[str, int], gcat: str) -> str:
    if not gitems:
        return f"no items ({gcat or '—'})"
    shown = sorted(gitems)[:2]
    more = "" if len(gitems) <= 2 else f" +{len(gitems) - 2}"
    return f"{len(gitems)} item(s): {', '.join(shown)}{more}"


def _submitted(sitems: dict[str, tuple[int, str]], scat: str, has_rows: bool) -> str:
    if sitems:
        return f"{len(sitems)} item(s): {', '.join(sorted(sitems)[:2])}"
    if scat:
        return f"no items ({scat})"
    return "missing line" if not has_rows else "nothing"


def _score_metrics(build: Build, golden, scorer) -> dict:
    if build.metrics.get("score") is not None:
        return build.metrics
    result = scorer.score_submission(golden, Path(build.path).read_bytes(), Path(build.path).name)
    return (result.get("metrics") or {}) if result.get("ok") else {}


def compare_builds(
    a: Build,
    b: Build,
    settings: Settings | None = None,
    examples: int = 5,
) -> Comparison:
    """Line-level and segment-level diff of two builds against the same golden.

    A line is *fixed* when it was wrong in `a` and is exactly right in `b` (same item
    set, amounts within tolerance, same exceptions). That is deliberately the strict
    scorer definition, not "closer": a tie-break that swaps one wrong item for another
    must not count as progress.
    """
    if a.phase != b.phase:
        raise ValueError(f"cannot compare phases '{a.phase}' and '{b.phase}'")
    golden, data, scorer = golden_for(settings, a.phase)
    text_by_txn = _load_text(data.dir)
    rows_a = read_submission(Path(a.path))
    rows_b = read_submission(Path(b.path))

    cmp = Comparison(
        a=a,
        b=b,
        metrics_a=_score_metrics(a, golden, scorer),
        metrics_b=_score_metrics(b, golden, scorer),
    )

    for txn in sorted(golden.txns):
        gitems_raw, gcat, gexc, segment = golden.txns[txn]
        gitems = {item: int(round(amount * 100)) for item, (amount, _exc) in gitems_raw.items()}
        gexc_by_item = {item: exc for item, (_amount, exc) in gitems_raw.items()}
        ra = rows_a.get(txn, [])
        rb = rows_b.get(txn, [])
        sa, cata, exca = parse_rows(ra)
        sb, catb, excb = parse_rows(rb)
        la = classify_line(gitems, gexc_by_item, gcat, gexc, sa, cata, exca, bool(ra))
        lb = classify_line(gitems, gexc_by_item, gcat, gexc, sb, catb, excb, bool(rb))

        cmp.lines += 1
        seg = cmp.segments.setdefault(segment or "unlabelled", {"n": 0, "fixed": 0, "broken": 0, "ok_a": 0, "ok_b": 0})
        seg["n"] += 1
        if la.ok:
            seg["ok_a"] += 1
        if lb.ok:
            seg["ok_b"] += 1

        if not la.ok:
            cmp.kinds_a.update(la.kinds)
            cmp.pairs_a["missing"] += len(la.missing)
            cmp.pairs_a["extra"] += len(la.extra)
            cmp.pairs_a["amount"] += len(la.off)
            cmp.pairs_a["exception"] += len(la.exc_wrong)
        if not lb.ok:
            cmp.kinds_b.update(lb.kinds)
            cmp.pairs_b["missing"] += len(lb.missing)
            cmp.pairs_b["extra"] += len(lb.extra)
            cmp.pairs_b["amount"] += len(lb.off)
            cmp.pairs_b["exception"] += len(lb.exc_wrong)

        if la.ok and lb.ok:
            cmp.still_right += 1
        elif not la.ok and not lb.ok:
            cmp.still_wrong += 1
        else:
            change = LineChange(
                txn=txn,
                segment=segment or "unlabelled",
                expected=_expected(gitems, gcat),
                before=_submitted(sa, cata, bool(ra)),
                after=_submitted(sb, catb, bool(rb)),
                text=_text_of(text_by_txn.get(txn)),
                kinds_before=la.kinds,
                kinds_after=lb.kinds,
            )
            if lb.ok:
                cmp.fixed_total += 1
                if len(cmp.fixed) < max(examples, 0):
                    cmp.fixed.append(change)
                seg["fixed"] += 1
            else:
                cmp.broken_total += 1
                if len(cmp.broken) < max(examples, 0):
                    cmp.broken.append(change)
                seg["broken"] += 1

    return cmp


# --------------------------------------------------------------------- render


def _delta(before: float | None, after: float | None) -> str:
    if before is None or after is None:
        return "—"
    return f"{after - before:+.4f}"


def _score(m: dict) -> float | None:
    value = m.get("score")
    return float(value) if value is not None else None


def render_comparison(cmp: Comparison, *, examples: int = 5) -> str:
    a, b = cmp.a, cmp.b
    ma, mb = cmp.metrics_a, cmp.metrics_b
    out: list[str] = []
    add = out.append

    def prov(build: Build) -> str:
        p = build.provenance or {}
        sha = p.get("code_sha") or "—"
        git = p.get("git") or {}
        if git.get("commit"):
            sha = f"{sha}@{git['commit']}{'*' if git.get('dirty') else ''}"
        return sha

    add("=" * 78)
    add("RUN COMPARISON")
    add("=" * 78)
    add(f"before  {a.id}  score {_score(ma) if _score(ma) is not None else '—'}  code {prov(a)}")
    if a.note:
        add(f"        note: {a.note}")
    add(f"after   {b.id}  score {_score(mb) if _score(mb) is not None else '—'}  code {prov(b)}")
    if b.note:
        add(f"        note: {b.note}")

    add("")
    add("Score terms")
    for key, label in (
        ("score", "score"),
        ("allocation_f1", "allocation F1"),
        ("precision", "  precision"),
        ("recall", "  recall"),
        ("txn_accuracy", "transaction acc"),
        ("exception_macro_f1", "exception F1"),
    ):
        before = ma.get(key)
        after = mb.get(key)
        add(f"  {label:<18} {before if before is not None else '—':>8} -> {after if after is not None else '—':>8}   {_delta(before, after)}")

    add("")
    add("Lines that changed (strict: exact item set + amounts + exceptions)")
    add(f"  fixed       {cmp.fixed_total:>7,}   wrong before, exactly right after")
    add(f"  broken      {cmp.broken_total:>7,}   right before, wrong after")
    add(f"  net         {cmp.net:>+7,}")
    add(f"  still wrong {cmp.still_wrong:>7,}   (not shown individually)")
    add(f"  still right {cmp.still_right:>7,}")

    add("")
    add("Allocation-pair loss")
    for key, label in (("missing", "missing (recall)"), ("extra", "extra (precision)"), ("amount", "amount"), ("exception", "exception (txn only)")):
        before = cmp.pairs_a.get(key, 0)
        after = cmp.pairs_b.get(key, 0)
        add(f"  {label:<22} {before:>9,} -> {after:>9,}   {after - before:+,}")

    moved = [
        (name, info)
        for name, info in cmp.segments.items()
        if info["fixed"] or info["broken"] or info["ok_a"] != info["ok_b"]
    ]
    if moved:
        add("")
        add("By segment (only where something moved)")
        add(f"  {'segment':<30} {'n':>7} {'fixed':>7} {'broken':>7} {'net':>7}  {'acc before':>10} -> {'acc after':<10}")
        for name, info in sorted(moved, key=lambda kv: -(abs(kv[1]["fixed"] - kv[1]["broken"]) + abs(kv[1]["ok_b"] - kv[1]["ok_a"]))):
            acc_a = info["ok_a"] / info["n"] if info["n"] else 0.0
            acc_b = info["ok_b"] / info["n"] if info["n"] else 0.0
            add(
                f"  {name:<30} {info['n']:>7,} {info['fixed']:>7,} {info['broken']:>7,} "
                f"{info['fixed'] - info['broken']:>+7,}  {acc_a:>10.3f} -> {acc_b:<10.3f}"
            )

    kinds = sorted(set(cmp.kinds_a) | set(cmp.kinds_b), key=lambda k: -abs(cmp.kinds_a.get(k, 0) - cmp.kinds_b.get(k, 0)))
    if kinds:
        add("")
        add("By failure kind (wrong lines)")
        add(f"  {'kind':<30} {'before':>8} {'after':>8}  {'delta':>8}")
        for kind in kinds:
            before, after = cmp.kinds_a.get(kind, 0), cmp.kinds_b.get(kind, 0)
            add(f"  {kind:<30} {before:>8,} {after:>8,}  {after - before:>+8,}")

    doc_a = a.documents or {}
    doc_b = b.documents or {}
    if doc_a and doc_b:
        family_changes = []
        for kind in sorted(set(doc_a.get("families", {})) | set(doc_b.get("families", {}))):
            fa = doc_a.get("families", {}).get(kind) or {}
            fb = doc_b.get("families", {}).get(kind) or {}
            if fa.get("entries", 0) == fb.get("entries", 0) and fa.get("total_mismatch", 0) == fb.get("total_mismatch", 0):
                continue
            family_changes.append(
                f"  {kind:<22} entries {fa.get('entries', 0):>8,} -> {fb.get('entries', 0):>8,}   "
                f"mismatched totals {fa.get('total_mismatch', 0):>4,} -> {fb.get('total_mismatch', 0):>4,}"
            )
        format_changes = []
        for fmt in sorted(set(doc_a.get("formats", {})) | set(doc_b.get("formats", {}))):
            fa = doc_a.get("formats", {}).get(fmt) or {}
            fb = doc_b.get("formats", {}).get(fmt) or {}
            if fa == fb:
                continue
            format_changes.append(
                f"  advice .{fmt:<12} files {fa.get('files', 0):>6,} -> {fb.get('files', 0):>6,}   "
                f"parsed {fa.get('parsed', 0):>6,} -> {fb.get('parsed', 0):>6,}   "
                f"no-extractor {fa.get('no_extractor', 0):>5,} -> {fb.get('no_extractor', 0):>5,}"
            )
        if family_changes or format_changes:
            add("")
            add("Preprocessing (what the document parsers extracted)")
            out.extend(family_changes)
            out.extend(format_changes)

    if cmp.broken:
        add("")
        add(f"Regressions ({len(cmp.broken)} shown) — right before, wrong after")
        for line in cmp.broken[:examples]:
            add(f"  {line.txn}  [{line.segment}]")
            add(f"    expected {line.expected}")
            add(f"    before   {line.before}")
            add(f"    after    {line.after}")
            if line.text:
                add(f"    text     {line.text!r}")

    return "\n".join(out)


def runs_table(builds: Builds, phase: str | None = None, baseline_id: str | None = None) -> str:
    """The experiment list, newest first, with delta against a pinned baseline."""
    entries = builds.list(phase)
    if not entries:
        return f"no builds in {builds.root} — run `bankrec process --phase {phase or 'sample'}`"
    evaluated = [b for b in entries if b.score is not None]
    baseline = builds.get(baseline_id) if baseline_id else (evaluated[-1] if evaluated else None)
    base_score = baseline.score if baseline is not None else None

    def prov(build: Build) -> str:
        p = build.provenance or {}
        sha = (p.get("code_sha") or "—")[:8]
        git = p.get("git") or {}
        if git.get("commit"):
            sha = f"{sha}{'*' if git.get('dirty') else ''}"
        return sha

    out = [
        f"{'build':<34} {'phase':<7} {'score':>6} {'Δbase':>7} {'lines':>7} {'code':<11} note",
    ]
    for build in entries:
        score = build.score
        delta = f"{score - base_score:+.2f}" if score is not None and base_score is not None and build.id != baseline.id else ""
        out.append(
            f"{build.id:<34} {build.phase:<7} "
            f"{(f'{score:.2f}' if score is not None else '—'):>6} {delta:>7} "
            f"{build.lines:>7,} {prov(build):<11} {build.note[:40]}"
        )
    if baseline is not None:
        out.append("")
        out.append(f"baseline  {baseline.id}  score {base_score if base_score is not None else '—'}")
    return "\n".join(out)


def _documents_markdown(documents: dict) -> list[str]:
    if not documents:
        return []
    lines = [
        "",
        "## Preprocessing",
        "",
        "| family | files | groups | entries | with reference | with both | sum≠declared |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for kind, family in (documents.get("families") or {}).items():
        lines.append(
            f"| {kind} | {family.get('files', 0):,} | {family.get('groups', 0):,} | "
            f"{family.get('entries', 0):,} | {family.get('with_reference', 0):,} | "
            f"{family.get('with_both', 0):,} | {family.get('total_mismatch', 0):,} |"
        )
    lines += [
        "",
        "| advice format | files | parsed | empty | failed | no extractor | entries |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for fmt, entry in (documents.get("formats") or {}).items():
        lines.append(
            f"| {fmt} | {entry.get('files', 0):,} | {entry.get('parsed', 0):,} | "
            f"{entry.get('empty', 0):,} | {entry.get('failed', 0):,} | "
            f"{entry.get('no_extractor', 0):,} | {entry.get('entries', 0):,} |"
        )
    warnings = documents.get("warnings") or []
    if warnings:
        lines += ["", "**Warnings**", ""]
        lines += [f"- {warning}" for warning in warnings]
    return lines


def markdown_report(build: Build, settings: Settings | None = None, *, examples: int = 3) -> str:
    """A portable report: provenance + score + the plain-English diagnosis."""
    from .insights import diagnose, render

    report = diagnose(build, settings, examples=examples)
    provenance = build.provenance or {}
    git = provenance.get("git") or {}
    data = provenance.get("data") or {}
    lines = [
        f"# {build.id}",
        "",
        f"| | |",
        f"|---|---|",
        f"| phase | `{build.phase}` |",
        f"| score | `{report.score if report.score is not None else '—'}` |",
        f"| rows / lines | {build.rows:,} / {build.lines:,} |",
        f"| note | {build.note or '—'} |",
        f"| tags | {', '.join(build.tags) or '—'} |",
        f"| git | `{git.get('commit') or '—'}`{' (dirty)' if git.get('dirty') else ''} |",
        f"| code sha | `{provenance.get('code_sha') or '—'}` |",
        f"| data manifest | `{data.get('manifest_sha') or '—'}` |",
        f"| artifact sha256 | `{build.sha256}` |",
        "",
    ]
    lines += _documents_markdown(build.documents)
    lines += [
        "",
        "## Score and diagnosis",
        "",
        "```",
        render(report, examples=examples),
        "```",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "Comparison",
    "LineChange",
    "capture_provenance",
    "code_fingerprint",
    "compare_builds",
    "data_fingerprint",
    "git_state",
    "markdown_report",
    "read_submission",
    "render_comparison",
    "runs_table",
]
