"""Turn a score into an explanation — join the golden with the submission, line by line.

The official scorer answers *where* you lose points (per-segment accuracy). It cannot
answer *why*, because it only ever looks at one submission at a time. This module diffs
the submission against the labels and names the failure in plain English:

    MISSING_ITEM               the line settles invoices the build did not name
    EXTRA_ITEM                 the build named invoices the line does not settle
    AMOUNT_OFF                 the right invoice was named with the wrong amount
    WRONG_EXCEPTION            right invoice and amount, wrong code (costs txn accuracy only)
    EXPECTED_ITEM_GOT_CATEGORY a real settlement was filed as a no-item line
    EXPECTED_CATEGORY_GOT_ITEM a no-item line was matched to an invoice
    WRONG_CATEGORY             both said "no item" but the category differs
    LINE_MISSING               the bank line is absent from the submission

Two different losses are reported separately, because they are fixed by different work:

  * allocation pairs  (missing / extra / amount)  -> allocation F1 (50 %)
  * whole lines       (exact item set + amount + exception) -> transaction accuracy (30 %)
  * exception classes (confusion per code)        -> exception macro-F1 (20 %)

Read-only by construction: it never writes to a build and never recomputes a score.
`evaluate --explain` calls it after the official score, so the number and the story
always come from the same labels (CLAUDE.md §13).
"""

from __future__ import annotations

import csv
import gzip
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .builds import Build
from .config import Settings
from .data import read_table
from .evaluate import golden_for
from .hooks import load_trace
from .schema import EXCEPTIONS, amount_tolerance_cents

# kind -> (plain-English meaning, which score term it damages)
KIND_INFO: dict[str, tuple[str, str]] = {
    "MISSING_ITEM": ("settles invoices the build did not name", "recall / F1"),
    "EXTRA_ITEM": ("names invoices this line does not settle", "precision / F1"),
    "AMOUNT_OFF": ("right invoice, wrong amount", "precision / F1 / txn"),
    "WRONG_EXCEPTION": ("right invoice and amount, wrong exception code", "txn / macro-F1"),
    "EXPECTED_ITEM_GOT_CATEGORY": ("a real settlement filed as a no-item line", "recall / txn"),
    "EXPECTED_CATEGORY_GOT_ITEM": ("a no-item line matched to an invoice", "precision / txn"),
    "WRONG_CATEGORY": ("both say 'no item' but the category differs", "txn"),
    "LINE_MISSING": ("the bank line is absent from the submission", "coverage"),
}

# order in which kinds are explained (biggest conceptual loss first)
KIND_ORDER = [
    "MISSING_ITEM",
    "EXPECTED_ITEM_GOT_CATEGORY",
    "EXTRA_ITEM",
    "AMOUNT_OFF",
    "EXPECTED_CATEGORY_GOT_ITEM",
    "WRONG_CATEGORY",
    "WRONG_EXCEPTION",
    "LINE_MISSING",
]


# ---------------------------------------------------------------------- model


@dataclass(slots=True)
class LineInsight:
    txn: str
    segment: str
    kinds: tuple[str, ...]
    expected: str
    submitted: str
    text: str = ""
    reason: str = "none"
    candidates: int = 0


@dataclass(slots=True)
class SegmentInsight:
    n: int = 0
    ok: int = 0
    kinds: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    wrong_reasons: Counter = field(default_factory=Counter)

    @property
    def accuracy(self) -> float:
        return self.ok / self.n if self.n else 0.0

    def main_failure(self) -> tuple[str, int]:
        return self.kinds.most_common(1)[0] if self.kinds else ("", 0)


@dataclass(slots=True)
class ExceptionInsight:
    golden: int = 0
    right: int = 0       # true positives (also counted in `submitted`)
    wrong: int = 0       # false positives: this code was emitted where it did not belong
    as_other: Counter = field(default_factory=Counter)  # golden class -> what we sent instead

    @property
    def submitted(self) -> int:
        return self.right + self.wrong

    @property
    def recall(self) -> float:
        return self.right / self.golden if self.golden else 0.0

    @property
    def precision(self) -> float:
        return self.right / self.submitted if self.submitted else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass(slots=True)
class InsightReport:
    build_id: str
    phase: str
    score: float | None = None
    metrics: dict = field(default_factory=dict)
    lines: int = 0
    lines_ok: int = 0
    kinds: Counter = field(default_factory=Counter)          # wrong lines per kind
    missing_pairs: int = 0
    extra_pairs: int = 0
    amount_off_pairs: int = 0
    exception_wrong_pairs: int = 0
    exception_wrong_lines: int = 0
    pairs_submitted: int = 0
    pairs_golden: int = 0
    segments: dict[str, SegmentInsight] = field(default_factory=dict)
    exceptions: dict[str, ExceptionInsight] = field(default_factory=dict)
    reason_all: Counter = field(default_factory=Counter)
    reason_wrong: Counter = field(default_factory=Counter)
    reason_wrong_pairs: Counter = field(default_factory=Counter)  # wrong item/amount/category
    reason_wrong_exc: Counter = field(default_factory=Counter)    # right pair, wrong exception only
    examples: dict[str, list[LineInsight]] = field(default_factory=lambda: defaultdict(list))
    trace: bool = False

    @property
    def wrong_lines(self) -> int:
        return self.lines - self.lines_ok

    def as_metrics(self) -> dict:
        """Small enough for builds/index.json, useful enough to track a trend."""
        return {
            "lines_ok": self.lines_ok,
            "missing_pairs": self.missing_pairs,
            "extra_pairs": self.extra_pairs,
            "amount_off_pairs": self.amount_off_pairs,
            "exception_wrong_pairs": self.exception_wrong_pairs,
            "failure_kinds": dict(self.kinds.most_common()),
            "exception_recall": {k: round(v.recall, 4) for k, v in sorted(self.exceptions.items())},
        }


# ---------------------------------------------------------------------- input


def _iter_rows(path: Path):
    magic = Path(path).read_bytes()[:2]
    if Path(path).suffix == ".gz" or magic == b"\x1f\x8b":
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as fh:
            yield from csv.reader(fh)
    else:
        with Path(path).open(encoding="utf-8-sig", newline="") as fh:
            yield from csv.reader(fh)


def _cents(value) -> int:
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _load_text(phase_dir: Path) -> dict[str, dict]:
    path = phase_dir / "bank_transactions.csv.gz"
    if not path.exists():
        return {}
    df = read_table(path)
    keep = [
        c
        for c in ("bank_txn_id", "booking_date", "amount", "currency", "text", "ref1", "ref2", "remittance_info", "counterparty_name")
        if c in df.columns
    ]
    return {row["bank_txn_id"]: row for row in df.select(keep).iter_rows(named=True)}


def _text_of(row: dict | None) -> str:
    if not row:
        return ""
    blob = " | ".join(
        str(row.get(k) or "") for k in ("text", "ref1", "ref2", "remittance_info") if row.get(k)
    )
    return blob[:110]


def _describe_items(items: dict[str, int], limit: int = 2) -> str:
    if not items:
        return "no items"
    shown = sorted(items)[:limit]
    more = "" if len(items) <= limit else f" +{len(items) - limit}"
    return f"{len(items)} item(s): {', '.join(shown)}{more}"


# ------------------------------------------------------------------- diagnose


def diagnose(
    build: Build,
    settings: Settings | None = None,
    examples: int = 3,
    metrics: dict | None = None,
) -> InsightReport:
    """Diff `build` against the golden of its phase and name every failure.

    `metrics` lets `evaluate --explain` pass the score it just computed (the build's
    sidecar is only updated after the report is printed).
    """
    golden, data, _scorer = golden_for(settings, build.phase)
    text_by_txn = _load_text(data.dir)
    trace = load_trace(build)

    submitted: dict[str, list[tuple[str, int, str, str]]] = defaultdict(list)
    reader = _iter_rows(Path(build.path))
    header = [h.strip().lower() for h in next(reader, [])]
    if header:
        ix = {name: i for i, name in enumerate(header)}
        for cells in reader:
            if not cells or not any(cells):
                continue
            try:
                txn = cells[ix["bank_txn_id"]].strip()
                item = cells[ix["item_id"]].strip()
                amount = _cents(cells[ix["amount"]]) if cells[ix["amount"]].strip() else 0
                category = cells[ix["category"]].strip()
                exception = cells[ix["exception"]].strip()
            except (KeyError, IndexError):
                continue
            submitted[txn].append((item, amount, category, exception))

    report = InsightReport(
        build_id=build.id,
        phase=build.phase,
        score=(metrics or {}).get("score"),
        metrics=dict(metrics or build.metrics or {}),
        trace=bool(trace),
        examples=defaultdict(list),
    )

    for txn in sorted(golden.txns):
        gitems_raw, gcat, gexc, segment = golden.txns[txn]
        gitems = {item: _cents(amount) for item, (amount, _exc) in gitems_raw.items()}
        gexc_by_item = {item: exc for item, (_amount, exc) in gitems_raw.items()}
        rows = submitted.get(txn, [])
        sitems = {item: (amount, exc) for item, amount, _cat, exc in rows if item}
        scat = next((cat for item, _a, cat, _e in rows if not item), "")
        sexc = next((exc for item, _a, _c, exc in rows if not item), "")

        report.lines += 1
        report.pairs_golden += len(gitems)
        report.pairs_submitted += len(sitems)

        kinds: list[str] = []
        missing: list[str] = []
        extra: list[str] = []
        off: list[str] = []
        exc_wrong: list[str] = []

        if gitems:
            if not rows:
                kinds.append("LINE_MISSING")
                missing = sorted(gitems)
            elif scat:
                kinds.append("EXPECTED_ITEM_GOT_CATEGORY")
                missing = sorted(gitems)
            else:
                missing = sorted(i for i in gitems if i not in sitems)
                extra = sorted(i for i in sitems if i not in gitems)
                for item, g in gitems.items():
                    if item not in sitems:
                        continue
                    sent = sitems[item][0]
                    if abs(sent - g) > amount_tolerance_cents(g):
                        off.append(item)
                if missing:
                    kinds.append("MISSING_ITEM")
                if extra:
                    kinds.append("EXTRA_ITEM")
                if off:
                    kinds.append("AMOUNT_OFF")
                for item in gitems:
                    if item in sitems and item not in off:
                        if sitems[item][1].upper() != (gexc_by_item[item] or "").upper():
                            exc_wrong.append(item)
                if exc_wrong:
                    kinds.append("WRONG_EXCEPTION")
        else:
            if not rows:
                kinds.append("LINE_MISSING")
            elif sitems:
                kinds.append("EXPECTED_CATEGORY_GOT_ITEM")
            elif scat.upper() != (gcat or "").upper():
                kinds.append("WRONG_CATEGORY")

        # line success mirrors the scorer exactly: same item set, every amount within
        # tolerance AND every exception equal (vendor/bankrec_score.py `st[1]`).
        if gitems:
            structural_ok = bool(rows) and not scat and not missing and not extra and not off
            ok = structural_ok and not exc_wrong
        else:
            structural_ok = bool(rows) and not sitems and scat.upper() == (gcat or "").upper()
            ok = structural_ok and sexc.upper() == (gexc or "").upper()
        # "exception only" is a very different failure: the allocation is right, so it
        # counts for F1 and loses only transaction accuracy / macro-F1.
        exc_only = (not ok) and structural_ok

        report.missing_pairs += len(missing)
        report.extra_pairs += len(extra)
        report.amount_off_pairs += len(off)
        report.exception_wrong_pairs += len(exc_wrong)
        report.exception_wrong_lines += 1 if exc_wrong else 0

        seg = report.segments.setdefault(segment or "unlabelled", SegmentInsight())
        seg.n += 1
        if ok:
            report.lines_ok += 1
            seg.ok += 1

        reason, candidates = trace.get(txn, ("none", 0))
        report.reason_all[reason] += 1
        seg.reasons[reason] += 1
        if not ok:
            report.reason_wrong[reason] += 1
            seg.wrong_reasons[reason] += 1
            if exc_only:
                report.reason_wrong_exc[reason] += 1
            else:
                report.reason_wrong_pairs[reason] += 1
            for kind in kinds:
                report.kinds[kind] += 1
                seg.kinds[kind] += 1

        _account_exceptions(report, gitems, gexc_by_item, sitems, gexc, sexc)

        if examples:
            if kinds or exc_wrong:
                line_kinds = tuple(kinds) or ("WRONG_EXCEPTION",)
                insight = LineInsight(
                    txn=txn,
                    segment=segment or "unlabelled",
                    kinds=line_kinds,
                    expected=(_describe_items(gitems) if gitems else f"no items ({gcat or '—'})"),
                    submitted=(
                        _describe_items({i: a for i, (a, _e) in sitems.items()})
                        if sitems
                        else (f"no items ({scat or '—'})" if rows else "missing line")
                    ),
                    text=_text_of(text_by_txn.get(txn)),
                    reason=reason,
                    candidates=candidates,
                )
                for kind in line_kinds:
                    bucket = report.examples[kind]
                    if len(bucket) < examples:
                        bucket.append(insight)

    return report


def _account_exceptions(
    report: InsightReport,
    gitems: dict[str, int],
    gexc_by_item: dict[str, str],
    sitems: dict[str, tuple[int, str]],
    gexc: str,
    sexc: str,
) -> None:
    """Reproduce the scorer's per-class TP/FP so macro-F1 can be explained, not just shown.

    Mirrors `vendor/bankrec_score.py`: a submitted code is a true positive when it names
    the golden exception of the same item (amount not required for this counter), and a
    false positive otherwise. Golden counts include every labelled item, so recall is
    `right / golden` exactly as the scorer computes it.
    """
    allowed = set(EXCEPTIONS)
    for item in gitems:
        ge = (gexc_by_item.get(item) or "").upper()
        if ge in allowed:
            report.exceptions.setdefault(ge, ExceptionInsight()).golden += 1
        if item not in sitems:
            continue
        se = sitems[item][1].upper()
        if se not in allowed:
            continue
        entry = report.exceptions.setdefault(se, ExceptionInsight())
        if se == ge:
            entry.right += 1
        else:
            entry.wrong += 1
            report.exceptions.setdefault(ge if ge in allowed else "(clean)", ExceptionInsight()).as_other[se] += 1
    # submitted exceptions on items the golden does not have (pure precision loss)
    for item, (_amount, se) in sitems.items():
        if item in gitems:
            continue
        se = se.upper()
        if se in allowed:
            report.exceptions.setdefault(se, ExceptionInsight()).wrong += 1
            report.exceptions.setdefault("(not-an-item)", ExceptionInsight()).as_other[se] += 1
    # no-item golden lines: the exception lives on the line, not on an item
    if not gitems:
        ge = (gexc or "").upper()
        if ge in allowed:
            report.exceptions.setdefault(ge, ExceptionInsight()).golden += 1
        se = (sexc or "").upper()
        if se in allowed:
            entry = report.exceptions.setdefault(se, ExceptionInsight())
            if se == ge:
                entry.right += 1
            else:
                entry.wrong += 1
                report.exceptions.setdefault(ge if ge in allowed else "(clean)", ExceptionInsight()).as_other[se] += 1


# --------------------------------------------------------------------- render


def _pct(part: int, total: int) -> str:
    return f"{part / total:5.1%}" if total else "  —  "


def render(report: InsightReport, *, worst: int = 12, examples: int = 0) -> str:
    m = report.metrics or {}
    out: list[str] = []
    add = out.append

    add("=" * 78)
    add("WHY THE SCORE IS WHAT IT IS")
    add(f"build {report.build_id}  ·  phase {report.phase}  ·  score {report.score if report.score is not None else '—'}")
    add("=" * 78)

    if m:
        add("")
        add("The three terms (100 × the weighted sum):")
        add(
            f"  allocation F1    {m.get('allocation_f1', 0):.3f}   "
            f"precision {m.get('precision', 0):.3f} / recall {m.get('recall', 0):.3f}"
            f"   {'← recall is the bottleneck' if m.get('recall', 0) < m.get('precision', 0) else ''}"
        )
        add(
            f"  transaction acc  {m.get('txn_accuracy', 0):.3f}   "
            f"{report.lines_ok:,}/{report.lines:,} lines exactly right"
        )
        add(f"  exception F1     {m.get('exception_macro_f1', 0):.3f}   mean over the classes below")

    add("")
    add("Where whole lines are lost")
    if not report.wrong_lines:
        add("  none — every line is exactly right")
    else:
        add(f"  {report.wrong_lines:,} of {report.lines:,} lines are wrong. Causes, biggest first (a line can have several):")
        for kind in sorted(KIND_ORDER, key=lambda k: -report.kinds.get(k, 0)):
            count = report.kinds.get(kind, 0)
            if not count:
                continue
            meaning, term = KIND_INFO[kind]
            add(f"    {count:>6,}  {_pct(count, report.wrong_lines)}  {kind:<28} {meaning}  [{term}]")

    add("")
    add("Where allocation pairs are lost (this is what F1 counts)")
    add(f"  golden pairs {report.pairs_golden:,}  ·  submitted {report.pairs_submitted:,}")
    add(f"    {report.missing_pairs:>6,} missing   the line settles invoices we never named  (hurts recall)")
    add(f"    {report.extra_pairs:>6,} extra     invoices we named that the line does not settle  (hurts precision)")
    add(f"    {report.amount_off_pairs:>6,} amount    right invoice, amount outside the 0.5 % tolerance")
    add(
        f"    {report.exception_wrong_pairs:>6,} exception pairs on {report.exception_wrong_lines:,} lines "
        f"have the right invoice and amount but the wrong code (costs txn accuracy, not F1)"
    )

    if report.segments:
        add("")
        add(f"Worst segments — work top-down (n × (1 − accuracy) is the damage)")
        if report.trace:
            add(f"  {'segment':<30} {'n':>7} {'acc':>6}  {'main failure':<26} what was chosen on wrong lines")
        else:
            add(f"  {'segment':<30} {'n':>7} {'acc':>6}  main failure")
        ranked = sorted(
            report.segments.items(),
            key=lambda kv: kv[1].n * (1 - kv[1].accuracy),
            reverse=True,
        )
        for name, seg in ranked[:worst]:
            if seg.n * (1 - seg.accuracy) <= 0:
                continue
            kind, count = seg.main_failure()
            main = f"{kind or '—':<24} ({count:,})"
            if report.trace:
                decided = seg.wrong_reasons.most_common(1)[0][0] if seg.wrong_reasons else "—"
                add(f"  {name:<30} {seg.n:>7,} {seg.accuracy:>6.2f}  {main:<26} {decided}")
            else:
                add(f"  {name:<30} {seg.n:>7,} {seg.accuracy:>6.2f}  {main}")

    if report.exceptions:
        add("")
        add("Exception classes — macro-F1 averages over these, so a rare class counts as much as PARTIAL")
        add(f"  {'class':<20} {'golden':>7} {'sent':>6} {'right':>6} {'recall':>7} {'prec':>6} {'F1':>6}  most often sent as")
        for name in sorted(report.exceptions, key=lambda k: report.exceptions[k].golden, reverse=True):
            entry = report.exceptions[name]
            if entry.golden == 0 and entry.submitted == 0:
                continue
            confusion = ", ".join(f"{k}({v})" for k, v in entry.as_other.most_common(2)) or "—"
            add(
                f"  {name:<20} {entry.golden:>7,} {entry.submitted:>6,} {entry.right:>6,} "
                f"{entry.recall:>7.2f} {entry.precision:>6.2f} {entry.f1:>6.2f}  {confusion}"
            )

    if report.trace:
        add("")
        add("How the matcher decided (from the --trace sidecar)")
        add("  item/amount = the allocation itself is wrong (hurts F1);  exception = right pair, wrong code")
        add(f"  {'reason':<24} {'lines':>9} {'wrong':>9} {'wrong %':>8} {'item/amount':>11} {'exception':>10}")
        for reason, total in sorted(report.reason_all.items(), key=lambda kv: -kv[1]):
            wrong = report.reason_wrong.get(reason, 0)
            add(
                f"  {reason:<24} {total:>9,} {wrong:>9,} {_pct(wrong, total):>8} "
                f"{report.reason_wrong_pairs.get(reason, 0):>11,} {report.reason_wrong_exc.get(reason, 0):>10,}"
            )
    else:
        add("")
        add("No decision trace for this build. Rebuild with `process --trace` to see which rule fired.")

    if examples and report.examples:
        add("")
        add(f"Examples ({examples} per failure kind)")
        for kind in KIND_ORDER:
            bucket = report.examples.get(kind) or []
            if not bucket:
                continue
            add(f"  {kind} — {KIND_INFO[kind][0]}")
            for line in bucket:
                seen = f" reason={line.reason}" + (f" candidates={line.candidates}" if line.candidates else "")
                add(f"    {line.txn}  [{line.segment}]{seen}")
                add(f"      expected  {line.expected}")
                add(f"      submitted {line.submitted}")
                if line.text:
                    add(f"      bank text {line.text!r}")
    return "\n".join(out)


def explain_build(
    build: Build,
    settings: Settings | None = None,
    *,
    examples: int = 3,
    worst: int = 12,
    metrics: dict | None = None,
) -> str:
    """Convenience wrapper: diagnose + render in one call."""
    report = diagnose(build, settings, examples=examples, metrics=metrics)
    return render(report, worst=worst, examples=examples)


__all__ = [
    "InsightReport",
    "LineInsight",
    "SegmentInsight",
    "ExceptionInsight",
    "KIND_INFO",
    "diagnose",
    "explain_build",
    "render",
]
