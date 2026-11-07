"""PDF -> facts, as a *stage* of the pipeline (see stages.py), not a decision.

The reconciliation works on CSV + state; a PDF can only change a decision through a
clause the CSVs do not carry (instalments, retention, an advance to deduct, "this
invoice replaces X", assignment, the month a recurring fee covers). So this package
extracts exactly those facts, plus the fields needed to check them, and nothing else.

    read.py      positions -> lines -> columns -> (label, value) pairs
    fields.py    money/date normalisation, label hints, field resolution, arithmetic
    clauses.py   anchored clause detectors (code + parameters + direction + negation)
    facts.py     the record + a lookup index the matcher can read
    build.py     phase -> facts cache (sha-keyed, incremental) + report

Nothing here decides anything: `build` produces evidence, `stages.record` records it,
and the engine opts in (`process --pdf-facts`). That keeps the score honest: a bad
clause can be switched off without touching the matcher.
"""

from .facts import DocFacts, PdfFacts, load, save
from .build import build_phase, report

__all__ = ["DocFacts", "PdfFacts", "load", "save", "build_phase", "report"]
