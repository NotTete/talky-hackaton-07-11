"""Why a settlement was missed: one cause per golden line the build filed without items.

`evaluate --explain` says *which kind* of failure a line has. This says *why the matcher
did not find the item*, using the probe the engine wrote into the trace (see hooks.py):

    no_trace          the build was not made with `process --trace`
    no_reference      the bank text carries no document number at all
    ref_open          a number matches an open item, but no step accepted it
    ref_settled       the number matches only items that are already settled
    ref_partial       the number matches only by prefix or suffix (truncation)
    ref_absent        the number matches nothing we know

A line can name several numbers; the cause is the best state among them, in the order
above, so `ref_open` beats `ref_absent` (the closer the evidence, the more it says).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .builds import Build
from .config import Settings
from .evaluate import golden_for
from .hooks import load_probes
from .insights import _cents, _iter_rows, _load_text, _text_of, parse_rows

CAUSES = ("no_trace", "no_reference", "ref_open", "ref_settled", "ref_partial", "ref_absent")
_RANK = {"open": 0, "settled": 1, "partial": 2, "absent": 3}


def cause_of(probe: dict | None) -> str:
    if probe is None:
        return "no_trace"
    tokens = probe.get("tok") or []
    if not tokens:
        return "no_reference"
    best = min(tokens, key=lambda t: _RANK.get(t[1], 9))[1]
    if best == "open":
        return "ref_open"
    return {"settled": "ref_settled", "partial": "ref_partial", "absent": "ref_absent"}[best]


@dataclass
class MissReport:
    build_id: str
    phase: str
    lines_missed: int = 0          # golden has items, the build filed none
    lines_golden_items: int = 0
    causes: Counter = field(default_factory=Counter)
    by_segment: dict = field(default_factory=lambda: defaultdict(Counter))
    examples: dict = field(default_factory=lambda: defaultdict(list))
    end_steps: Counter = field(default_factory=Counter)
    payer_known: int = 0           # missed lines whose bank text names a known payer


def missed_settlements(build: Build, settings: Settings | None = None, examples: int = 3) -> MissReport:
    golden, data, _scorer = golden_for(settings, build.phase)
    probes = load_probes(build)
    text_by_txn = _load_text(data.dir)

    submitted: dict[str, list] = defaultdict(list)
    reader = _iter_rows(Path(build.path))
    header = [h.strip().lower() for h in next(reader, [])]
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

    report = MissReport(build_id=build.id, phase=build.phase)
    for txn in sorted(golden.txns):
        gitems_raw, _gcat, _gexc, segment = golden.txns[txn]
        if not gitems_raw:
            continue
        report.lines_golden_items += 1
        sitems, _scat, _sexc = parse_rows(submitted.get(txn, []))
        if sitems:
            continue
        report.lines_missed += 1
        probe = probes.get(txn)
        cause = cause_of(probe)
        report.causes[cause] += 1
        report.by_segment[segment][cause] += 1
        if probe is not None:
            report.end_steps[str(probe.get("end", "none"))] += 1
            if probe.get("parties", 0) > 0:
                report.payer_known += 1
        if len(report.examples[cause]) < examples:
            report.examples[cause].append(
                (txn, segment, probe, _text_of(text_by_txn.get(txn)), _describe(golden.txns[txn][0]))
            )
    return report


def _describe(gitems_raw: dict) -> str:
    items = sorted(gitems_raw)
    more = "" if len(items) <= 2 else f" +{len(items) - 2}"
    return f"{len(items)} item(s): {', '.join(items[:2])}{more}"


def render(report: MissReport, *, examples: int = 3) -> str:
    out = [
        f"build {report.build_id} ({report.phase})",
        f"golden lines with items: {report.lines_golden_items:,}   "
        f"filed with no items (missed): {report.lines_missed:,}",
    ]
    if not report.lines_missed:
        out.append("nothing missed.")
        return "\n".join(out)
    if report.causes.get("no_trace"):
        out.append("some lines have no trace: rebuild with `process --trace` for the causes.")
    out.append("")
    out.append("cause                     lines   share")
    for cause in CAUSES:
        n = report.causes.get(cause, 0)
        if n:
            out.append(f"  {cause:<22} {n:>6}   {100 * n / report.lines_missed:5.1f}%")
    out.append(f"payer named in the bank text (known to the masters): {report.payer_known:,}")
    out.append("")
    out.append("by segment (top 8 by missed lines)")
    ranked = sorted(report.by_segment.items(), key=lambda kv: -sum(kv[1].values()))[:8]
    for segment, counts in ranked:
        parts = ", ".join(f"{c}={counts[c]}" for c in CAUSES if counts.get(c))
        out.append(f"  {segment:<26} {sum(counts.values()):>5}   {parts}")
    if examples:
        out.append("")
        out.append("examples")
        for cause in CAUSES:
            for txn, segment, probe, text, golden_items in report.examples.get(cause, [])[:examples]:
                out.append(f"  [{cause}] {txn} [{segment}] golden {golden_items}")
                out.append(f"      bank text: {text}")
                if probe:
                    out.append(f"      references: {probe.get('tok')}")
                    out.append(f"      steps: {probe.get('steps')}  ended: {probe.get('end')}")
    return "\n".join(out)


__all__ = ["CAUSES", "cause_of", "missed_settlements", "render"]
