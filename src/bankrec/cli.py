"""Command line: `python -m bankrec <command>`.

Three steps, three commands — on purpose:

    process    build a submission and store it as an immutable, named artifact
    evaluate   score it (TRAIN/sample) or check its coverage (LIVE/FINAL)
    push       upload a *specific, evaluated* build — the only irreversible action

Everything else is support: `builds` lists the repository, `validate` is the structural
pre-flight, `doctor` checks the data layout.

    python -m bankrec process  --phase sample --name baseline
    python -m bankrec evaluate <build-id>            # or the latest one
    python -m bankrec push LIVE <build-id>
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .builds import Build, Builds
from .config import ROOT, load_settings
from .evaluate import evaluate_build, format_segments, metrics_for, validate_file
from .experiments import compare_builds, markdown_report, render_comparison, runs_table
from .pipeline import process
from .schema import CATEGORIES, EXCEPTIONS, SUBMISSION_COLUMNS, validate_row  # noqa: F401 (re-export)

PHASES = ["sample", "train", "train-lite", "live", "final"]


def _settings(args: argparse.Namespace):
    s = load_settings(args.config)
    if args.data:
        s.data_dir = Path(args.data)
    if getattr(args, "builds_dir", None):
        s.builds_dir = Path(args.builds_dir)
    if getattr(args, "phase", None):
        s.phase = args.phase
    if getattr(args, "item_phases", None):
        s.item_phases = [p.strip() for p in args.item_phases.split(",") if p.strip()]
    return s


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, help="alternative settings.toml")
    p.add_argument("--data", type=Path, help="data root (default: data/)")
    p.add_argument("--builds-dir", type=Path, help="build repository (default: builds/)")
    p.add_argument("--phase", choices=PHASES)
    p.add_argument("--item-phases", help="comma separated item tables, e.g. live")


def _resolve(builds: Builds, ref: str | None, phase: str | None) -> Build:
    if ref:
        build = builds.get(ref)
        if build is None:
            path = Path(ref)
            if path.exists():  # a file that was not built by us
                return Build(
                    id=path.stem,
                    phase=phase or "unknown",
                    path=str(path),
                    sha256="",
                    bytes=path.stat().st_size,
                    created_at="",
                )
            raise SystemExit(f"unknown build: {ref} (see `bankrec builds`)")
        return build

    build = builds.latest(phase)
    if build is None:
        hint = f" for {phase}" if phase else ""
        raise SystemExit(f"no builds{hint} yet — run `bankrec process --phase {phase or 'sample'}`")
    return build


# ------------------------------------------------------------------ process


def cmd_process(args) -> int:
    settings = _settings(args)
    if getattr(args, "pdf_facts", False):
        settings.use_pdf_facts = True
    hooks = []
    if args.trace:
        from .hooks import TraceRecorder

        hooks.append(TraceRecorder(_settings(args).builds_dir))
    build = process(
        settings,
        args.phase,
        limit=args.limit,
        name=args.name,
        engine=args.engine,
        hooks=hooks,
        note=args.note,
        tags=args.tag,
    )
    print(f"build   {build.id}")
    print(f"file    {build.path}")
    print(f"sha256  {build.sha256}")
    print(f"rows    {build.rows:,} ({build.item_rows:,} item + {build.no_item_rows:,} category)")
    if build.provenance:
        git = build.provenance.get("git") or {}
        print(f"code    {build.provenance.get('code_sha', '—')}@git:{git.get('commit') or '—'}{' (dirty)' if git.get('dirty') else ''}")
    if build.trace:
        print(f"trace   {build.trace}")
    print("\nnext:   bankrec evaluate " + build.id + (" --explain" if build.trace else ""))
    return 0


# ----------------------------------------------------------------- evaluate


def cmd_evaluate(args) -> int:
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    build = _resolve(builds, args.build, args.phase)

    scored, coverage = evaluate_build(build, settings)
    print(f"build   {build.id}  ({build.phase})")
    print(f"file    {build.path}")
    if scored is not None:
        print(scored.summary())
        metrics = metrics_for(build, scored, coverage)
        if not metrics.get("valid"):
            print(coverage.summary())
        if report := format_segments(metrics):
            print(report)
        if args.explain and scored.ok:
            from .insights import diagnose, render

            insight = diagnose(build, settings, examples=max(args.examples, 0), metrics=metrics)
            metrics["diagnosis"] = insight.as_metrics()
            print()
            print(render(insight, examples=max(args.examples, 0)))
    else:
        print(f"no golden for '{build.phase}' — coverage only:")
        print(coverage.summary())
        metrics = metrics_for(build, None, coverage)

    builds.mark_evaluated(build.id, metrics)
    print(f"\nrecorded in {builds.index_path}")
    if not metrics.get("valid", True):
        return 1
    return 0


# --------------------------------------------------------------------- push


def _push_warnings(build: Build, builds: Builds, phase: str) -> list[str]:
    """Everything a human should know before spending a submission."""
    out: list[str] = []
    out.append(f"build      {build.id}  ({build.rows:,} rows, sha256 {build.sha256[:12]}…)")
    if build.phase and build.phase.lower() != phase.lower():
        out.append(f"WARNING    this build was made for '{build.phase}', not '{phase.lower()}'")

    latest = builds.latest(phase)
    if latest and latest.id != build.id:
        out.append(f"WARNING    a newer build exists for {phase}: {latest.id} — you are pushing an older one")

    if not build.evaluated:
        out.append("WARNING    this build was never evaluated (no coverage check, no score)")
    elif build.metrics.get("valid") is False:
        out.append("WARNING    this build FAILED validation — the server would reject it")

    last = builds.last_pushed(phase)
    if last is None:
        out.append(f"last push  none for {phase}")
    elif last.id == build.id and build.pushed:
        out.append(f"WARNING    this exact build was already pushed at {build.pushed.get('at')}")
    elif last.sha256 == build.sha256:
        out.append(
            f"WARNING    identical content was already pushed at {last.pushed.get('at')} "
            f"(score {last.pushed.get('score')})"
        )
    else:
        out.append(
            f"last push  {last.id} at {last.pushed.get('at')} "
            f"(score {last.pushed.get('score')}, status {last.pushed.get('status')})"
        )

    if phase.upper() == "FINAL":
        out.append("WARNING    FINAL allows exactly ONE scored submission per team — this cannot be undone")
    return out


def cmd_whoami(args) -> int:
    """Check the team token against /api/me. Uses no upload slot, uploads nothing."""
    from .submit import whoami

    print(json.dumps(whoami(api=args.api or ""), indent=2, ensure_ascii=False))
    return 0


def cmd_result(args) -> int:
    """Read back what the platform recorded: submissions list, or one submission."""
    from .submit import result, submissions

    data = result(args.sub_id, api=args.api) if args.sub_id else submissions(api=args.api)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_push(args) -> int:
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    phase = args.phase.upper()
    build = _resolve(builds, args.build, phase.lower())

    lines = _push_warnings(build, builds, phase.lower())
    print(f"phase      {phase}")
    print("\n".join(lines))
    print(f"file       {build.path}", flush=True)

    if not build.file.exists():
        raise SystemExit(f"missing file: {build.path}")
    from .submit import submit

    # submit() owns the human gate (typed phrase on a TTY, see tests/test_submit_guard.py).
    # There is no flag that skips it: see CLAUDE.md 15.1.
    result = submit(phase, build.file)
    builds.mark_pushed(build.id, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nrecorded push in {builds.index_path}")
    return 0


# ---------------------------------------------------------------- pdf-facts


def cmd_pdf_facts(args) -> int:
    from .data import Phase
    from .pdf.build import build_phase, report as facts_report
    from .pdf.facts import from_pdf
    from .stages import record

    settings = _settings(args)
    if args.explain:
        doc = from_pdf(Path(args.explain), entity=args.entity or "", doc_type="")
        print(json.dumps(doc.as_row(), indent=2, ensure_ascii=False))
        return 0

    phase = Phase(settings, args.phase)
    facts, report = build_phase(phase, limit=args.limit, force=args.rebuild, workers=args.workers)
    print(report.summary())
    if args.record:
        metrics = {
            "documents": report.documents,
            "with_number": report.with_number,
            "with_total": report.with_total,
            "arithmetic_ok": report.arithmetic_ok,
            "parsed": report.parsed,
            "cached": report.cached,
            "seconds": round(report.seconds, 2),
            **{f"clause_{k}": v for k, v in (report.clauses or {}).items()},
            **{f"flag_{k}": v for k, v in (report.flags or {}).items()},
        }
        run = record(
            "pdf_facts",
            report.phase,
            settings=settings,
            note=args.note,
            metrics=metrics,
            reports=[Path(report.path), Path(report.path).with_name("pdf_facts.meta.json")],
            warnings=[f"{k}={v}" for k, v in (report.flags or {}).items()],
        )
        print(f"\nrecorded stage {run.id}")
    return 0


# ------------------------------------------------------------------- builds


def cmd_builds(args) -> int:
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    entries = builds.list(args.phase)
    if not entries:
        print(f"no builds in {builds.root} — run `bankrec process --phase sample`")
        return 0
    print(f"{'build':<34} {'phase':<6} {'score':>6} {'state':<6} rows / lines")
    for build in entries:
        exists = "" if build.file.exists() else "  (file missing)"
        print(build.summary() + exists)
    if args.json:
        print(json.dumps({b.id: asdict(b) for b in entries}, indent=2))
    return 0


def cmd_runs(args) -> int:
    """The experiment list: score, delta vs a baseline, code version, hypothesis."""
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    print(runs_table(builds, args.phase, args.baseline))
    return 0


def _baseline(builds: Builds, phase: str | None):
    done = [b for b in builds.list(phase) if b.score is not None]
    return done[-1] if done else None


def cmd_compare(args) -> int:
    """Diff two builds: which lines were fixed/broken, and which segments moved."""
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    phase = args.phase

    if args.a and args.b:
        before = _resolve(builds, args.a, phase)
        after = _resolve(builds, args.b, phase)
    elif args.a:
        after = _resolve(builds, args.a, phase)
        before = _baseline(builds, after.phase)
    else:
        after = builds.latest(phase)
        before = _baseline(builds, after.phase if after else phase)

    if before is None or after is None:
        raise SystemExit("need two builds: `bankrec compare [BEFORE] AFTER` (or evaluate a baseline first)")
    if before.id == after.id:
        raise SystemExit(f"both picks are the same build ({before.id})")
    try:
        comparison = compare_builds(before, after, settings, examples=max(args.examples, 0))
    except FileNotFoundError as exc:
        raise SystemExit(f"cannot compare without labels: {exc}") from None
    print(render_comparison(comparison, examples=max(args.examples, 0)))
    return 0


def cmd_report(args) -> int:
    """A portable markdown report: provenance + score + why."""
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    build = _resolve(builds, args.build, args.phase)
    try:
        text = markdown_report(build, settings, examples=max(args.examples, 0))
    except FileNotFoundError as exc:
        raise SystemExit(f"cannot explain without labels: {exc}") from None
    if args.md:
        stem = Path(build.path).name.split(".")[0]
        out = Path(build.path).with_name(f"{stem}.md")
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


def cmd_documents(args) -> int:
    """Preprocessing report: what each document family gave us, and what is still raw."""
    settings = _settings(args)
    from .data import Phase
    from .documents import build_document_report, render
    from .preprocess import DocumentContext, load_advices

    phase = Phase(settings, args.phase)
    context = DocumentContext(phase)
    advices = load_advices(phase)
    txns = None if args.no_attach else phase.bank_transactions()
    report = build_document_report(phase, context, advices, txns=txns)
    print(render(report, attached=not args.no_attach))
    return 0


def cmd_stages(args) -> int:
    """List preprocessing stage runs (PDF layout, advice parsing, ...)."""
    settings = _settings(args)
    from .stages import Stages, compare_runs, render_table, stages_root

    runs = Stages(stages_root(settings)).list(args.stage, args.phase)
    print(render_table(runs))
    if args.compare:
        if len(runs) < 2:
            print("\nneed two runs of the same stage to compare")
        else:
            print(compare_runs(runs[1], runs[0]))
    return 0


def cmd_stage(args) -> int:
    """Record a preprocessing run: metrics + report files + provenance."""
    settings = _settings(args)
    from .stages import format_metrics, record

    metrics: dict = {}
    for spec in args.metric or []:
        if "=" not in spec:
            raise SystemExit(f"--metric expects key=value, got {spec!r}")
        key, value = spec.split("=", 1)
        try:
            parsed: object = float(value)
        except ValueError:
            parsed = value
        metrics[key.strip()] = parsed
    run = record(
        args.stage_name,
        args.phase or settings.phase,
        settings=settings,
        note=args.note,
        tags=args.tag,
        metrics=metrics,
        reports=[Path(path) for path in (args.report or [])],
        warnings=args.warning,
    )
    print(f"stage   {run.id}")
    print(f"metrics {format_metrics(run.metrics)}")
    print(f"reports {', '.join(r['name'] for r in run.reports) or '—'}")
    if run.warnings:
        print(f"warnings {len(run.warnings)}: {'; '.join(run.warnings[:3])}")
    print("\nlist:   bankrec stages")
    return 0


# ----------------------------------------------------------------- validate


def cmd_validate(args) -> int:
    settings = _settings(args)
    builds = Builds(settings.builds_dir)
    build = _resolve(builds, args.build, args.phase)
    coverage = validate_file(build.file, settings, build.phase)
    print(f"build {build.id}  ({build.phase})")
    print(coverage.summary())
    if not coverage.ok:
        print(f"\n{len(coverage.errors)} problem(s); the server would reject this file")
        return 1
    print("OK — the official scorer should accept this file")
    return 0


# ------------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    settings = _settings(args)
    print(f"bankrec {__version__}")
    print(f"project {ROOT}")
    print(f"data    {settings.data_dir} ({'ok' if settings.data_dir.exists() else 'MISSING'})")
    if not settings.data_dir.exists():
        print("  -> make data")
        return 1
    for phase in PHASES:
        d = settings.data_dir / phase
        print(
            f"  {phase:<10} dir={d.exists()!s:<5} "
            f"bank_txns={(d / 'bank_transactions.csv.gz').exists()!s:<5} "
            f"golden={(d / 'golden_allocations.csv.gz').exists()!s:<5} "
            f"open_items={(d / 'open_items_at_start.csv.gz').exists()}"
        )
    masters = settings.data_dir / "masters"
    needed = ["customers", "suppliers", "bank_accounts", "payment_terms", "fx_rates", "categories"]
    missing = [n for n in needed if not (masters / f"{n}.csv").exists()]
    print(f"  masters    {'ok' if not missing else 'missing: ' + ', '.join(missing)}")

    builds = Builds(settings.builds_dir)
    entries = builds.list()
    print(f"builds  {settings.builds_dir} ({len(entries)} build(s))")
    for build in entries[:5]:
        print("  " + build.summary())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bankrec", description="Talky x Anthropic — bank reconciliation (process / evaluate / push)"
    )
    parser.add_argument("--version", action="version", version=f"bankrec {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("process", aliases=["run", "build"], help="build a submission artifact")
    _common(p)
    p.add_argument("--name", default="", help="optional label appended to the build id")
    p.add_argument("--limit", type=int, default=0, help="only the first N bank lines (dev)")
    p.add_argument("--pdf-facts", action="store_true",
                   help="use the PDF facts cache as extra evidence (see `bankrec pdf-facts`)")
    p.add_argument(
        "--engine",
        choices=["alt", "baseline"],
        default="alt",
        help="alt: operation-first (bankrec.alt); baseline: the conservative matcher",
    )
    p.add_argument("--trace", action="store_true", help="record which rule fired per line (evaluate --explain reads it)")
    p.add_argument("--note", default="", help="hypothesis for this experiment (shown by `runs`)")
    p.add_argument("--tag", action="append", default=[], help="label for grouping runs (repeatable)")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("evaluate", aliases=["score"], help="score / coverage-check a build")
    p.add_argument("build", nargs="?", help="build id or file (default: latest for the phase)")
    p.add_argument("--explain", action="store_true", help="plain-English report: where the lines and pairs are lost")
    p.add_argument("--examples", type=int, default=3, help="example lines per failure kind with --explain")
    _common(p)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("push", aliases=["submit"], help="upload a build to LIVE/FINAL")
    p.add_argument("phase", choices=["LIVE", "FINAL", "live", "final"])
    p.add_argument("build", nargs="?", help="build id (default: latest for the phase)")

    _common(p)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("result", aliases=["status"], help="read back submitted scores")
    p.add_argument("sub_id", nargs="?", help="submission id (default: list them all)")
    p.add_argument("--api", default="", help="override the API base URL")
    p.set_defaults(func=cmd_result)

    p = sub.add_parser("whoami", help="check HK_TOKEN against /api/me (uploads nothing)")
    p.add_argument("--api", default="", help="override the API base URL")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("pdf-facts", help="build/refresh the PDF facts cache and report")
    _common(p)
    p.add_argument("--limit", type=int, default=0, help="only the first N PDFs (dev)")
    p.add_argument("--rebuild", action="store_true", help="re-parse even when the sha is unchanged")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--record", action="store_true", help="record the run in the stage registry")
    p.add_argument("--note", default="")
    p.add_argument("--explain", type=Path, help="print the facts of one PDF and exit")
    p.add_argument("--entity", default="", help="entity for --explain")
    p.set_defaults(func=cmd_pdf_facts)

    p = sub.add_parser("builds", aliases=["ls"], help="list the build repository")
    _common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_builds)

    p = sub.add_parser("runs", aliases=["experiments"], help="experiment list: score, Δbaseline, code, note")
    _common(p)
    p.add_argument("--baseline", help="build id to measure against (default: oldest evaluated)")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("compare", aliases=["diff"], help="diff two builds: fixed/broken lines and segment deltas")
    p.add_argument("a", nargs="?", help="before (or the only build: compared to the baseline)")
    p.add_argument("b", nargs="?", help="after (default: the baseline→latest pair)")
    p.add_argument("--examples", type=int, default=5, help="regressions to show")
    _common(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("report", help="markdown report: provenance + score + plain-English diagnosis")
    p.add_argument("build", nargs="?", help="build id (default: latest)")
    p.add_argument("--md", action="store_true", help="write <build>.md next to the artifact")
    p.add_argument("--examples", type=int, default=3)
    _common(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("documents", aliases=["preprocess", "docs"], help="preprocessing report: parsed documents, integrity, attachment")
    _common(p)
    p.add_argument("--no-attach", action="store_true", help="skip the bank-line scan (structure only)")
    p.set_defaults(func=cmd_documents)

    p = sub.add_parser("stages", aliases=["stage-runs"], help="list preprocessing stage runs")
    _common(p)
    p.add_argument("--stage", help="filter by stage name")
    p.add_argument("--compare", action="store_true", help="compare the two latest runs (metrics + reports)")
    p.set_defaults(func=cmd_stages)

    p = sub.add_parser("stage", aliases=["record-stage"], help="record a preprocessing run (metrics + report files)")
    _common(p)
    p.add_argument("--stage", dest="stage_name", required=True, help="stage name, e.g. pdf_layout")
    p.add_argument("--note", default="", help="what the run tested")
    p.add_argument("--tag", action="append", default=[], help="label (repeatable)")
    p.add_argument("--metric", action="append", default=[], help="key=value (repeatable)")
    p.add_argument("--report", action="append", default=[], help="report file the stage wrote (repeatable)")
    p.add_argument("--warning", action="append", default=[], help="what the stage could not do (repeatable)")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("validate", help="structural pre-flight of a build")
    p.add_argument("build", nargs="?", help="build id (default: latest)")
    _common(p)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("doctor", help="check data layout, config and builds")
    _common(p)
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
