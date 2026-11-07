"""Preprocessing report: what did each document family actually give us, and does it add up?

Matching has `--explain`; preprocessing needs its own report for the same reason: a
silent drop (a file that fails to parse, a batch whose entries do not sum to the
declared total, a payment run that never attaches to a bank line) is invisible in the
final score and looks like a matching bug. This module answers, per phase:

  * structure   — files -> groups -> entries, and how many entries carry a usable
                  document reference / amount (cheap; computed during `process`)
  * integrity   — do the entries sum to the batch/deposit total the bank moved?
  * formats     — CSV/TXT parsed vs XLSX/PDF with no extractor (the handoff list)
  * attachment  — how many bank lines the document index can explain, and which
                  documents were never claimed (requires a scan of the lines)

It never matches items: that is the engine's job (`alt/engine.py`), and `--explain` reports it there.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .data import Phase
from .preprocess import AdviceIndex, DocumentContext
from .schema import amount_tolerance_cents

# kind -> folder on disk
FAMILY_FOLDERS = {
    "dd_batch": "direct_debit_batches",
    "card": "card_settlements",
    "cheque": "cheque_deposits",
    "payment_run": "payment_runs",
}

# Which columns make a group entry usable (the engine reads the same ones).
# `(reference fields, amount field)`.
GROUP_ENTRY_FIELDS = {
    "dd_batch": (("remittance_info", "mandate_id"), "amount_cents"),
    "card": (("order_number",), "gross_cents"),
    "cheque": (("stub_reference",), "amount_cents"),
    "payment_run": (("document_number",), "transfer_cents"),
}

# Only these families carry a *declared* total we can check the entries against.
DECLARED_TOTAL_FIELDS = {"dd_batch": "batch_total_cents", "cheque": "deposit_total_cents"}


def _amount(entry: dict, field_name: str) -> int:
    try:
        return abs(int(entry.get(field_name) or 0))
    except (TypeError, ValueError):
        return 0


def _has_reference(entry: dict, fields: tuple[str, ...]) -> bool:
    return any(str(entry.get(name) or "").strip() for name in fields)


# ---------------------------------------------------------------------- model


@dataclass(slots=True)
class FamilyReport:
    kind: str
    files: int = 0
    groups: int = 0
    entries: int = 0
    with_amount: int = 0
    with_reference: int = 0
    with_both: int = 0
    total_mismatch: int = 0
    matched_lines: int = 0
    matched_groups: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def ref_ratio(self) -> float:
        return self.with_reference / self.entries if self.entries else 0.0

    @property
    def both_ratio(self) -> float:
        return self.with_both / self.entries if self.entries else 0.0


@dataclass(slots=True)
class FormatReport:
    fmt: str
    files: int = 0
    parsed: int = 0
    empty: int = 0
    failed: int = 0
    no_extractor: int = 0
    entries: int = 0
    with_reason: int = 0
    with_deduction: int = 0
    matched_lines: int = 0

    @property
    def usable_by_matching(self) -> bool:
        return self.entries > 0


@dataclass(slots=True)
class DocumentReport:
    phase: str
    families: dict[str, FamilyReport] = field(default_factory=dict)
    formats: dict[str, FormatReport] = field(default_factory=dict)
    bank_lines: int = 0
    group_lines: int = 0
    advice_lines: int = 0
    unmatched_groups: int = 0
    unmatched_advices: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_metrics(self) -> dict:
        """Compact enough for the build sidecar; full detail is rendered on demand."""
        return {
            "families": {
                kind: {
                    "files": f.files,
                    "groups": f.groups,
                    "entries": f.entries,
                    "with_reference": f.with_reference,
                    "with_both": f.with_both,
                    "total_mismatch": f.total_mismatch,
                }
                for kind, f in sorted(self.families.items())
            },
            "formats": {
                fmt: {
                    "files": f.files,
                    "parsed": f.parsed,
                    "empty": f.empty,
                    "failed": f.failed,
                    "no_extractor": f.no_extractor,
                    "entries": f.entries,
                }
                for fmt, f in sorted(self.formats.items())
            },
            "warnings": list(self.warnings),
        }


# ----------------------------------------------------------------- structure


def structure(phase: Phase, context: DocumentContext, advices: list) -> DocumentReport:
    """Files / groups / entries / integrity + advice formats. No bank-line scan."""
    report = DocumentReport(phase=phase.name or "")

    for kind, folder in FAMILY_FOLDERS.items():
        directory = phase.dir / folder
        files = len(list(directory.glob("*.csv"))) if directory.is_dir() else 0
        family = FamilyReport(kind=kind, files=files)
        ref_fields, amount_field = GROUP_ENTRY_FIELDS[kind]
        declared_field = DECLARED_TOTAL_FIELDS.get(kind)
        for group in context.groups:
            if group.kind != kind:
                continue
            family.groups += 1
            computed = 0
            for entry in group.entries:
                family.entries += 1
                amount = _amount(entry, amount_field)
                computed += amount
                has_amount = amount > 0
                has_ref = _has_reference(entry, ref_fields)
                family.with_amount += has_amount
                family.with_reference += has_ref
                family.with_both += has_amount and has_ref
            if declared_field and group.entries:
                declared = _amount(group.entries[0], declared_field)
                if declared and abs(declared - computed) > 1:
                    family.total_mismatch += 1
                    if len(family.examples) < 5:
                        family.examples.append(
                            f"{group.account_id} {group.ymd}: declared {declared / 100:,.2f} "
                            f"vs entries {computed / 100:,.2f}"
                        )
        report.families[kind] = family
        if family.files and not family.groups:
            report.warnings.append(f"{folder}: {family.files} file(s) but no document groups extracted")
        if family.groups and family.both_ratio < 0.5:
            report.warnings.append(
                f"{folder}: only {family.both_ratio:.0%} of entries have both a reference and an amount"
            )

    for advice in advices:
        fmt = (getattr(advice, "fmt", "") or Path(advice.path).suffix.lstrip(".") or "?").lower()
        entry = report.formats.setdefault(fmt, FormatReport(fmt=fmt))
        entry.files += 1
        if advice.error == "no extractor":
            entry.no_extractor += 1
        elif advice.error:
            entry.failed += 1
        elif not advice.entries:
            entry.empty += 1
        else:
            entry.parsed += 1
        for line in advice.entries:
            entry.entries += 1
            entry.with_reason += bool(str(line.reason or "").strip())
            entry.with_deduction += line.deduction_cents > 0

    for fmt, entry in report.formats.items():
        if entry.no_extractor:
            report.warnings.append(f"remittance advices .{fmt}: {entry.no_extractor} file(s) have no extractor")
        if entry.failed:
            report.warnings.append(f"remittance advices .{fmt}: {entry.failed} file(s) could not be parsed")

    for kind, family in report.families.items():
        if family.total_mismatch:
            report.warnings.append(
                f"{FAMILY_FOLDERS[kind]}: {family.total_mismatch} group(s) where entries != declared total"
            )
    return report


# ----------------------------------------------------------------- attachment


def attach(
    report: DocumentReport,
    context: DocumentContext,
    advices: AdviceIndex | None,
    txns,
) -> DocumentReport:
    """Scan the bank lines and count how many the document index can explain.

    This is the join-health check: a parsed batch that no bank line ever claims is as
    useless as an unparsed one. It is deliberately read-only — no item is settled.
    """
    matched_by_kind: dict[str, set[int]] = defaultdict(set)
    matched_advices: set[int] = set()
    group_lines = Counter()
    advice_lines = 0
    report.bank_lines = len(txns)

    for txn in txns.iter_rows(named=True):
        cash = abs(int(txn["amount_cents"]))
        tol = amount_tolerance_cents(cash)
        group = context.group_for(txn, tol)
        if group is not None:
            group_lines[group.kind] += 1
            matched_by_kind[group.kind].add(id(group))
        if advices is not None and len(advices):
            payer_blob = " ".join(
                str(txn.get(key) or "") for key in ("counterparty_name", "text", "remittance_info")
            )
            ymd = int(str(txn.get("booking_date") or "0").replace("-", "")[:8] or 0)
            advice = advices.find(payer_blob, cash, ymd, tol)
            if advice is not None and not getattr(advice, "error", ""):
                advice_lines += 1
                matched_advices.add(id(advice))
                fmt = (getattr(advice, "fmt", "") or Path(advice.path).suffix.lstrip(".") or "?").lower()
                if fmt in report.formats:
                    report.formats[fmt].matched_lines += 1

    for kind, family in report.families.items():
        family.matched_lines = group_lines.get(kind, 0)
        family.matched_groups = len(matched_by_kind.get(kind, set()))

    report.group_lines = sum(group_lines.values())
    report.advice_lines = advice_lines
    report.unmatched_groups = sum(f.groups - f.matched_groups for f in report.families.values())
    report.unmatched_advices = max(len(advices) - len(matched_advices), 0) if advices is not None else 0
    if report.unmatched_groups:
        report.warnings.append(f"{report.unmatched_groups} document group(s) are never claimed by a bank line")
    if report.unmatched_advices:
        report.warnings.append(f"{report.unmatched_advices} advice file(s) never matched a bank line")
    return report


# --------------------------------------------------------------------- render


def render(report: DocumentReport, *, attached: bool = False) -> str:
    out: list[str] = []
    add = out.append
    add("=" * 78)
    add("PREPROCESSING REPORT")
    add(f"phase {report.phase}")
    add("=" * 78)

    add("")
    add("Document families (files -> groups -> entries)")
    if attached:
        add(f"  {'family':<22} {'files':>6} {'groups':>7} {'entries':>8} {'ref':>8} {'both':>8} {'sum≠decl':>9} {'matched':>8}")
    else:
        add(f"  {'family':<22} {'files':>6} {'groups':>7} {'entries':>8} {'ref':>8} {'both':>8} {'sum≠decl':>9}")
    for kind in FAMILY_FOLDERS:
        family = report.families.get(kind)
        if family is None:
            continue
        row = (
            f"  {FAMILY_FOLDERS[kind]:<22} {family.files:>6,} {family.groups:>7,} {family.entries:>8,} "
            f"{family.with_reference:>8,} {family.with_both:>8,} {family.total_mismatch:>9,}"
        )
        if attached:
            row += f" {family.matched_groups:>8,}"
        add(row)

    add("")
    add("Remittance advice formats")
    add(f"  {'format':<8} {'files':>7} {'parsed':>7} {'empty':>6} {'failed':>7} {'no-extr':>8} {'entries':>8} {'reason':>7} {'deduct':>7}")
    for fmt in sorted(report.formats):
        entry = report.formats[fmt]
        add(
            f"  {fmt:<8} {entry.files:>7,} {entry.parsed:>7,} {entry.empty:>6,} {entry.failed:>7,} "
            f"{entry.no_extractor:>8,} {entry.entries:>8,} {entry.with_reason:>7,} {entry.with_deduction:>7,}"
        )

    if attached:
        add("")
        add(f"Attachment to {report.bank_lines:,} bank lines")
        if report.bank_lines:
            add(f"  explained by a document group    {report.group_lines:>9,}  ({report.group_lines / report.bank_lines:.1%})")
            add(f"  explained by a remittance advice {report.advice_lines:>9,}  ({report.advice_lines / report.bank_lines:.1%})")
        else:
            add("  no bank lines supplied")
        add(f"  groups never claimed             {report.unmatched_groups:>9,}")
        add(f"  advice files never used          {report.unmatched_advices:>9,}")

    if report.warnings:
        add("")
        add("Warnings")
        for warning in report.warnings:
            add(f"  ! {warning}")

    examples = [(kind, family) for kind, family in report.families.items() if family.examples]
    if examples:
        add("")
        add("Integrity examples (entries do not sum to the declared total)")
        for kind, family in examples:
            add(f"  {FAMILY_FOLDERS[kind]}")
            for example in family.examples:
                add(f"    {example}")
    return "\n".join(out)


def build_document_report(
    phase: Phase,
    context: DocumentContext,
    advices: list,
    txns=None,
) -> DocumentReport:
    """Structure always; attachment only when the bank lines are supplied."""
    report = structure(phase, context, advices)
    if txns is not None:
        index = AdviceIndex(advices) if advices else None
        attach(report, context, index, txns)
    return report


__all__ = [
    "DocumentReport",
    "FamilyReport",
    "FormatReport",
    "attach",
    "build_document_report",
    "render",
    "structure",
]
