#!/usr/bin/env python3
"""Rebuild `pipeline-map.html`: one technical sheet about the pipeline.

    python scripts/build_map.py               # write pipeline-map.html
    python scripts/build_map.py --check       # exit 1 when the page is stale
    python scripts/build_map.py --serve       # localhost:8000, rebuild + reload on save

The sheet is a diagram (scripts/map_diagram.py) plus the reference notes for each stage.
Every number and claim is derived, never typed:

  * closed lists, weights, limits, tolerance  <- src/bankrec/schema.py (imported)
  * which codes are reachable                 <- AST scan of src/bankrec/rules.py
  * rows, score, per-segment accuracy         <- scoring an existing submission
  * blocks and flows                          <- scripts/map_diagram.py (geometry checked)
  * styling                                   <- scripts/map_theme.css (a real stylesheet)

A stage that names a symbol that no longer exists fails the build, and so does a diagram
whose boxes overlap or whose text overflows. A map that can go stale is worse than none.
"""

from __future__ import annotations

import argparse
import ast
import csv
import gzip
import html
import json
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from bankrec import rules, schema  # noqa: E402
from bankrec.builds import Builds  # noqa: E402
from bankrec.config import load_settings  # noqa: E402
from bankrec.data import Phase  # noqa: E402
from bankrec.evaluate import score_file  # noqa: E402
from bankrec.builds import Builds  # noqa: E402
from bankrec.submit import describe  # noqa: E402
import map_diagram  # noqa: E402

OUT = ROOT / "pipeline-map.html"
THEME = ROOT / "scripts" / "map_theme.css"

# Stage table. `symbols` are imported before the page is written, so a renamed function
# breaks the build instead of the documentation.
STAGES = [
    {
        "id": "load",
        "title": "Load and canonicalise",
        "src": "src/bankrec/data.py",
        "detail": "Phase · Masters · FX · to_cents",
        "symbols": ["data:Phase", "data:Masters", "data:FX", "data:to_cents"],
    },
    {
        "id": "index",
        "title": "Index the open items",
        "src": "src/bankrec/matching.py",
        "detail": "ItemIndex",
        "symbols": ["matching:ItemIndex", "matching:Allocation"],
    },
    {
        "id": "candidates",
        "title": "Propose candidates",
        "src": "src/bankrec/matching.py",
        "detail": "Matcher.match",
        "symbols": ["matching:Matcher", "text:reference_tokens", "text:ref_variants"],
    },
    {
        "id": "allocate",
        "title": "Allocate the cash",
        "src": "src/bankrec/matching.py",
        "detail": "Matcher._rows · _sum",
        "symbols": ["matching:Matcher._rows", "matching:Matcher._sum"],
    },
    {
        "id": "explain",
        "title": "Name the difference",
        "src": "src/bankrec/rules.py",
        "detail": "exception_for · category_for",
        "symbols": ["rules:exception_for", "rules:category_for", "rules:CATEGORY_KEYWORDS"],
    },
    {
        "id": "score",
        "title": "Process, validate, evaluate",
        "src": "src/bankrec/pipeline.py",
        "detail": "process · evaluate.score_file · hooks · insights",
        "symbols": [
            "pipeline:process",
            "evaluate:score_file",
            "schema:validate_row",
            "hooks:DecisionHook",
            "insights:diagnose",
        ],
    },
    {
        "id": "ship",
        "title": "Ship, after a human says so",
        "src": "src/bankrec/cli.py",
        "detail": "push · submit.submit · _gate · describe",
        "symbols": ["submit:submit", "submit:describe", "submit:_gate"],
    },
]

EXCEPTION_NOTES = {
    "PARTIAL": "allocation always applies the whole open amount, so the remainder is never positive",
    "OVERPAYMENT": "same reason: applied is never above open",
    "SHORT_DISCOUNT": "needs payment terms and the three day grace window",
    "SHORT_BANK_CHARGES": "shortfall within 60 EUR and 2 percent of the item",
    "SHORT_WITHHOLDING": "10, 1.5 or 1.25 percent of the invoice total",
    "SHORT_DISPUTE": "needs the advice text, which is not parsed",
    "DUPLICATE_PAYMENT": "a book state, not a row property",
    "UNIDENTIFIED": "also a category; never used on an item row",
    "RETURNED": "re-opens an item that a collection already closed",
    "FX_DIFFERENCE": "item and line currency differ and the item clears",
}

SEGMENT_NEEDS = {
    "ap_payment_multi": "expand payment_runs/",
    "ap_payment_single": "expand payment_runs/",
    "ar_1toN_text": "split the remittance text",
    "ar_same_amount_fifo": "oldest-first inside the party",
    "ar_short_discount": "terms + 3 day grace",
    "ar_dd_returned": "re-open on return",
    "ar_1toN_credit_note": "net the credit note",
    "ar_instalment": "PARTIAL across lines",
    "ar_partial": "emit PARTIAL at all",
    "ar_n_to_m": "subset sum",
    "ar_card_settlement": "expand card_settlements/",
    "ar_cheque_deposit": "expand cheque_deposits/",
    "ar_dd_batch": "expand direct_debit_batches/",
    "ar_1toN_remittance": "parse remittance_advices/",
    "ar_short_withholding": "withholding rates",
    "ap_credit_note_netted": "net the credit note",
    "ar_cheque_returned": "re-open on return",
    "ar_overpayment": "cash above open",
    "ap_partial": "emit PARTIAL at all",
    "ar_retention": "retention schedule",
    "nonitem_bank_fees": "already covered: keyword table",
    "nonitem_bank_fees_intl": "already covered: keyword table",
    "ar_1to1": "already covered: party gate",
    "ar_ref_noise": "already covered: reference_tokens",
    "ar_same_amount_month": "already covered: unique amount",
}


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------
@dataclass
class Facts:
    phase: str
    submission: Path | None = None
    error: str = ""
    lines: int = 0
    rows: int = 0
    item_rows: int = 0
    no_item_rows: int = 0
    items: int = 0
    bytes: int = 0
    score: float = 0.0
    digest: str = ""
    metrics: dict = field(default_factory=dict)
    segments: dict = field(default_factory=dict)
    exceptions: Counter = field(default_factory=Counter)
    categories: Counter = field(default_factory=Counter)
    reachable: set = field(default_factory=set)

    @property
    def ready(self) -> bool:
        return not self.error and bool(self.metrics)

    @property
    def dead_codes(self) -> list[str]:
        return [c for c in schema.EXCEPTIONS if not self.exceptions.get(c)]

    @property
    def silent_codes(self) -> list[str]:
        return [c for c in sorted(self.reachable) if not self.exceptions.get(c)]


def reachable_exceptions() -> set[str]:
    """Non-empty string constants returned from rules.exception_for (AST, not grep)."""
    tree = ast.parse((ROOT / "src" / "bankrec" / "rules.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "exception_for":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                    if isinstance(sub.value.value, str) and sub.value.value:
                        found.add(sub.value.value)
    return found


def verify_stages() -> None:
    import importlib

    missing = []
    for stage in STAGES:
        for ref in stage["symbols"]:
            module_name, _, attr = ref.partition(":")
            obj = importlib.import_module(f"bankrec.{module_name}")
            for part in attr.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    missing.append(ref)
                    break
    if missing:
        raise SystemExit(
            "the map references symbols that no longer exist: "
            + ", ".join(sorted(set(missing)))
            + "\nfix STAGES in scripts/build_map.py (or restore the symbol)"
        )


def read_submission(path: Path) -> tuple[Counter, Counter, int, int]:
    """Exceptions and categories actually emitted, plus the two row shapes."""
    exc: Counter = Counter()
    cat: Counter = Counter()
    item_rows = no_item_rows = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("item_id"):
                item_rows += 1
                if row.get("exception"):
                    exc[row["exception"]] += 1
            else:
                no_item_rows += 1
                if row.get("category"):
                    cat[row["category"]] += 1
    return exc, cat, item_rows, no_item_rows


def collect(phase: str, submission: Path | None = None) -> Facts:
    facts = Facts(phase=phase)
    facts.reachable = reachable_exceptions()

    settings = load_settings()
    settings.phase = phase
    # LIVE/FINAL carry their own open items (see data.Phase.items); TRAIN needs train.
    settings.item_phases = [phase]

    latest = Builds(settings.builds_dir).latest(phase)
    path = submission or (latest.file if latest else None)
    facts.submission = path if path is not None and path.exists() else None
    if facts.submission is None:
        facts.error = f"no {phase} build in {settings.builds_dir}; run `make process PHASE={phase}` first"
        return facts

    try:
        facts.items = len(Phase(settings, phase).items())
    except Exception as exc:  # data not downloaded: the sheet still renders
        facts.items = 0
        facts.error = f"cannot read the item frame: {exc}"

    report = score_file(facts.submission, settings, phase)
    facts.metrics = dict(report.metrics)
    facts.segments = dict(report.segments)
    facts.score = float(report.score or 0.0)
    facts.rows = int(facts.metrics.get("rows", 0))
    facts.lines = int(facts.metrics.get("txns_total", 0))
    info = describe(facts.submission)
    facts.digest = info["sha256"]
    facts.bytes = info["uncompressed_bytes"]
    exc, cat, item_rows, no_item_rows = read_submission(facts.submission)
    facts.exceptions, facts.categories = exc, cat
    facts.item_rows, facts.no_item_rows = item_rows, no_item_rows
    return facts


# ---------------------------------------------------------------------------
# html helpers
# ---------------------------------------------------------------------------
def e(value) -> str:
    return html.escape(str(value), quote=True)


def n(value) -> str:
    return f"{value:,}"


def section_head(stage: dict, number: int) -> str:
    return f"""    <div class="sec-head">
      <span class="num">{number}</span>
      <h2>{e(stage['title'])}</h2>
      <span class="src"><b>{e(stage['src'])}</b> {e(stage['detail'])}</span>
    </div>"""


def note(kind: str, body: str) -> str:
    label = {"info": "note", "inv": "invariant", "void": "not built", "fail": "failure mode"}[kind]
    return f'      <div class="note note-{kind}"><dt>{label}</dt><dd>{body}</dd></div>'


def notes(*blocks: str) -> str:
    return '    <div class="notes">\n' + "\n".join(blocks) + "\n    </div>"


def table(caption: str, head: list[str], rows: list[str]) -> str:
    head_html = "".join(f"<th>{h}</th>" for h in head)
    body = "\n".join(f"          <tr>{r}</tr>" for r in rows)
    return f"""      <table class="tbl">
        <caption>{caption}</caption>
        <thead><tr>{head_html}</tr></thead>
        <tbody>
{body}
        </tbody>
      </table>"""


def missing(stage: dict, number: int, facts: "Facts") -> str:
    """A stage with nothing measured says so, instead of printing zeros."""
    return f"""<section class="sec" id="{stage['id']}">
  <div class="wrap">
{section_head(stage, number)}
    <div class="note note-info"><dt>no numbers on this build</dt><dd>{e(facts.error) or "Run the phase once, then rebuild this sheet."}</dd></div>
  </div>
</section>"""


def bar(value: float, scale: float, soft: bool = False) -> str:
    pct = max(0.4, min(100.0, 100.0 * value / scale)) if scale else 0.4
    return f'<span class="bar{" soft" if soft else ""}" style="width:{pct:.1f}%"></span>'


# ---------------------------------------------------------------------------
# page parts
# ---------------------------------------------------------------------------
def header(facts: Facts) -> str:
    return f"""<header class="head">
  <div class="head-top">
    <span><b>bankrec</b> / pipeline map</span>
    <span class="right">
      <span>phase {e(facts.phase)}</span>
      <span>{len(STAGES)} stages</span>
      <span>{"measured" if facts.ready else "no numbers"}</span>
      <span>scripts/build_map.py</span>
    </span>
  </div>
  <h1>How a bank line becomes a submission row</h1>
  <p class="abstract">
    One pass over the bank lines of a phase. A line ends as one or more item rows, or as a
    single category row; nothing is skipped, because an omitted line counts as wrong. The
    diagram shows the blocks and the direction of data. Each stage below gives the rule, the
    file it lives in, and what it currently scores.
  </p>
</header>"""


def nav() -> str:
    links = "".join(f'<a href="#{s["id"]}">{e(s["id"])}</a>' for s in STAGES)
    return f"""<nav class="nav">
  <div class="nav-in">
    <span>fig. 1</span>
    {links}
    <a href="#levers">levers</a>
  </div>
</nav>"""


def spec(facts: Facts) -> str:
    if not facts.ready:
        return f"""<dl class="spec">
    <div><dt>state</dt><dd>no numbers</dd></div>
    <div><dt>reason</dt><dd class="wide">{e(facts.error)}</dd></div>
  </dl>"""
    m = facts.metrics
    return f"""<dl class="spec">
    <div><dt>phase</dt><dd>{e(facts.phase)}</dd></div>
    <div><dt>bank lines</dt><dd>{n(facts.lines)}</dd></div>
    <div><dt>rows out</dt><dd>{n(facts.rows)}<small>{n(facts.item_rows)} item · {n(facts.no_item_rows)} cat</small></dd></div>
    <div><dt>score</dt><dd>{facts.score:.2f}<small>of 100</small></dd></div>
    <div><dt>precision / recall</dt><dd>{m['precision']:.3f}<small>{m['recall']:.3f}</small></dd></div>
    <div><dt>csv sha256</dt><dd>{facts.digest[:12]}<small>{facts.bytes / 1024:.0f} kB</small></dd></div>
  </dl>"""


def figure(facts: Facts) -> str:
    fig, report = map_diagram.build_fig()
    graph = {
        "edges": [{"a": a, "b": b} for a, b in map_diagram.EDGES],
        "loop": list(map_diagram.LOOP),
    }
    fallback = "".join(
        f'      <li><b>{e(node[3])}</b> {e(" · ".join(node[4]))}</li>\n'
        for node in map_diagram.NODES
    )
    return f"""<section class="figwrap" aria-label="Pipeline diagram">
  <figure class="fig">
    <div class="fig-cap"><b>fig. 1</b><span>blocks and flows, left to right</span><span class="right">{e(report)}</span></div>
{fig.render()}
    <div class="fig-legend">
      <span><i class="sw solid"></i>executed</span>
      <span><i class="sw dashed"></i>declared in POLICIES.md, not wired</span>
      <span><i class="sw accent"></i>state written back into the index</span>
      <span class="hint">hover a block to trace it · click to jump to its notes</span>
    </div>
    <ul class="fig-list">
{fallback}    </ul>
    <p class="fig-src"><b>source</b> scripts/map_diagram.py · positions are a 6 x 6 grid, edges are routed into gutter channels, and the build fails if a box overlaps or a line runs through a box</p>
  </figure>
  <script type="application/json" id="graph">{json.dumps(graph)}</script>
</section>"""


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------
def stage_load(facts: Facts) -> str:
    stage = STAGES[0]
    cols = table(
        "canonical item columns (one frame, four sources)",
        ["column", "type", "meaning"],
        [
            '<td class="mono">item_id</td><td>str</td><td>ARI/ARC/API/APC + year + sequence</td>',
            '<td class="mono">side · kind</td><td>str</td><td>AR or AP, invoice or credit note</td>',
            '<td class="mono">entity · party_id</td><td>str</td><td>who owes or is owed</td>',
            '<td class="mono">document_number</td><td>str</td><td>the number that arrives mangled in the bank text</td>',
            '<td class="mono">issue_date · due_date</td><td>str</td><td>YYYY-MM-DD, compared as integers for FX</td>',
            '<td class="mono">currency</td><td>str</td><td>the item\'s currency, not the account\'s</td>',
            '<td class="mono">total_cents · open_cents</td><td>int</td><td>integer cents; open is the mutable one</td>',
            '<td class="mono">payment_terms</td><td>str</td><td>key into <code>Masters.terms</code></td>',
        ],
    )
    return f"""<section class="sec" id="load">
  <div class="wrap">
{section_head(stage, 1)}
    <div class="sec-grid">
{notes(
    note("inv", "Money is an integer from <code>to_cents</code> onwards. That function is the only place a float exists, and the tolerance below it is <code>max(0.01, 0.5%)</code> in integer cents."),
    note("info", f"Bank lines are sorted by <code>(booking_date, bank_txn_id)</code> once, at load. PARTIAL, RETURNED and DUPLICATE_PAYMENT are properties of a moment, so order is part of the data model, not a convenience."),
    note("info", f"Every master-derived lookup is built in <code>Masters.__init__</code>: {len(rules.CATEGORY_KEYWORDS)} category keywords, terms, IBAN owners, own accounts, party names. Nothing O(masters) runs inside the per-line loop."),
)}
{cols}
    </div>
  </div>
</section>"""


def stage_index(facts: Facts) -> str:
    stage = STAGES[1]
    if not facts.ready:
        return missing(stage, 2, facts)
    structures = table(
        "ItemIndex: built once, queried per line",
        ["structure", "key → value", "changes"],
        [
            '<td class="mono">items</td><td>item_id → row dict</td><td class="acc">never</td>',
            '<td class="mono">open</td><td>item_id → open cents</td><td class="live">every settle</td>',
            '<td class="mono">by_ref</td><td>document variant → item ids</td><td class="acc">never</td>',
            '<td class="mono">by_party</td><td>party id → item ids</td><td class="acc">never</td>',
            '<td class="mono">party_by_token</td><td>name token → party ids</td><td class="acc">never</td>',
            '<td class="mono">_by_amount</td><td>open cents → item ids, plus a sorted key list</td><td class="live">every settle</td>',
        ],
    )
    return f"""<section class="sec" id="index">
  <div class="wrap">
{section_head(stage, 2)}
    <div class="sec-grid">
{notes(
    note("info", f"One pass over {n(facts.items)} items converts the polars frame into dicts. After that the per-line loop never touches a DataFrame: the candidate set is a handful of dict lookups."),
    note("inv", "<code>open_cents</code> is the only mutable state. <code>settle()</code> decrements it, floors at zero, and patches the amount index with <code>bisect.insort</code> / delete. Nothing is rebuilt, so lookups stay logarithmic."),
    note("fail", "Anything that batches lines out of order loses the exceptions that depend on sequence: a returned direct debit re-opens an item, and a second payment can only be duplicate because the first one closed the item."),
)}
{structures}
    </div>
  </div>
</section>"""


def stage_candidates(facts: Facts) -> str:
    stage = STAGES[2]
    if not facts.ready:
        return missing(stage, 3, facts)
    gates = table(
        "gates, in precision order: first one that reconciles wins",
        ["#", "evidence", "accepts", "on miss"],
        [
            '<td class="mono live">1</td><td>document reference in the text</td><td class="mono">all referenced items, if they sum to the cash</td><td>falls through</td>',
            '<td class="mono live">2</td><td>counterparty IBAN, then name tokens + amount</td><td class="mono">one item</td><td>falls through</td>',
            '<td class="mono live">3</td><td>amount unique across all open items</td><td class="mono">that item</td><td>no candidate</td>',
            '<td class="mono dead">4</td><td>documents: advices, dd batches, settlements, runs</td><td class="mono dead">not loaded</td><td class="dead">declared only</td>',
        ],
    )
    return f"""<section class="sec" id="candidates">
  <div class="wrap">
{section_head(stage, 3)}
    <div class="sec-grid">
{notes(
    note("info", f"Each gate asks the same question with weaker evidence. The order is a precision ranking, not a filter: a gate that finds items it cannot reconcile falls through instead of returning nothing, because a candidate that is never proposed can never be recovered."),
    note("info", "Reference variants are generated per document number (leading zeros, digit core) and SEPA vocabulary is dropped, so <code>EREF/NOTPROVIDED/RECHNUNG</code> do not look like invoice numbers."),
    note("fail", f"Gate 1 returns only when the referenced items sum to the cash, so 1:N remittances reach gate 3 and usually fail there. Recall is {facts.metrics.get('recall', 0):.3f} against precision {facts.metrics.get('precision', 0):.3f}: the missing points are all on the proposal side."),
)}
{gates}
    </div>
  </div>
</section>"""


def stage_allocate(facts: Facts) -> str:
    stage = STAGES[3]
    code = """      <div class="term">
        <div class="term-top"><b>matching.py</b><span>shortened</span></div>
        <pre><span class="o"># gate 1: propose, check the sum, only then touch the book</span>
rows = self._rows(txn, referenced, settle=False)
<span class="a">if</span> self._sum(rows, ccy, ymd) &lt;= cash + tol:
    self._commit(rows)              <span class="o"># now the balances move</span>
    <span class="a">return</span> rows
<span class="o"># else: nothing was settled, items stay open for later branches</span>

<span class="o"># _rows(settle=True) is the default: every other caller still emits</span>
<span class="o"># AND settles in one step, because they never need to undo a guess</span>
<span class="a">def</span> _commit(self, rows):
    <span class="a">for</span> row <span class="a">in</span> rows:
        self.index.settle(row.item_id, row.cents)</pre>
      </div>"""
    return f"""<section class="sec" id="allocate">
  <div class="wrap">
{section_head(stage, 4)}
    <div class="sec-grid">
{notes(
    note("inv", "Emission and settlement are the same step everywhere except gate 1: a caller that already knows its candidate is right (unique amount, a group entry, an advice line) settles inside <code>_rows</code>, so a later line sees the balance this one left behind."),
    note("info", "Gate 1 is the one place a candidate set can be wrong (a reference that does not sum to the cash), so it alone defers: <code>_rows(..., settle=False)</code> builds the rows, the sum check decides, and only <code>_commit</code> touches the book. A referenced set that does not add up now falls through with every balance untouched."),
    note("void", "Subset sum over the candidates, oldest-first FIFO for repeated identical amounts, credit note netting, and instalment chains. All of them are allocation changes, not candidate changes, which is why they belong to one function."),
)}
{code}
    </div>
  </div>
</section>"""


def stage_explain(facts: Facts) -> str:
    stage = STAGES[4]
    if not facts.ready:
        return missing(stage, 5, facts)
    order = sorted(
        schema.EXCEPTIONS,
        key=lambda c: (not facts.exceptions.get(c), schema.EXCEPTIONS.index(c)),
    )
    rows = []
    for name in order:
        hits = facts.exceptions.get(name, 0)
        reachable = "yes" if name in facts.reachable else "no"
        rows.append(
            f'<td class="mono {"live" if hits else "dead"}">{e(name)}</td>'
            f'<td class="mono {"acc" if reachable == "yes" else "dead"}">{reachable}</td>'
            f'<td class="n">{n(hits)}</td>'
            f'<td class="wrap">{e(EXCEPTION_NOTES.get(name, ""))}</td>'
        )
    exc_table = table(
        f"exception codes (closed list of {len(schema.EXCEPTIONS)})",
        ["code", "in code", "emitted", "why"],
        rows,
    )

    internal = facts.categories.get("INTERNAL_TRANSFER", 0) + facts.categories.get("INTERCOMPANY", 0)
    fallback = facts.categories.get("UNIDENTIFIED", 0) + facts.categories.get("BANK_FEES", 0)
    resolved = max(facts.no_item_rows - fallback - internal, 0)
    cat_rows = table(
        "category cascade, first match wins",
        ["order", "test", "result", "rows"],
        [
            f'<td class="mono live">1</td><td>counterparty is one of our IBANs</td><td class="mono">INTERNAL_TRANSFER · INTERCOMPANY</td><td class="n">{n(internal)}</td>',
            f'<td class="mono live">2</td><td>folded text against {len(rules.CATEGORY_KEYWORDS)} keyword families</td><td class="mono">one of {len({c for c, _ in rules.CATEGORY_KEYWORDS})} named categories</td><td class="n">{n(resolved)}</td>',
            f'<td class="mono live">3</td><td>sign of the line</td><td class="mono">UNIDENTIFIED · BANK_FEES</td><td class="n">{n(fallback)}</td>',
        ],
    )
    return f"""<section class="sec" id="explain">
  <div class="wrap">
{section_head(stage, 5)}
    <div class="sec-grid">
{notes(
    note("info", "Two closed lists, one per row shape: an item row carries an exception, a no-item row carries a category. The server rejects the whole file for an unknown value, so both lists are constants and both are validated before upload."),
    note("info", f"Default is the empty code: a wrong code costs the whole transaction, a blank one only costs the code. <code>exception_for</code> is always called with a zero remainder, so its currency test is the only branch that can fire" + (f"; the code does return {' and '.join(facts.silent_codes)}, but nothing reaches it." if facts.silent_codes else ".")),
    note("info", f"Categories are the fallback for lines that settle nothing: {n(fallback)} of {n(facts.no_item_rows)} no-item rows end in the sign default, so most of the category accuracy comes from a two-value guess, not from the keyword table."),
)}
{exc_table}
{cat_rows}
    </div>
  </div>
</section>"""


def stage_score(facts: Facts) -> str:
    stage = STAGES[5]
    if not facts.ready:
        return missing(stage, 6, facts)
    m = facts.metrics
    shapes = table(
        "the only three row shapes the writer emits",
        ["bank_txn_id", "item_id", "amount", "category", "exception"],
        [
            '<td class="mono">BT-2025-0001234</td><td class="mono">ARI-2025-0013456</td><td class="mono">1210.00</td><td class="dead">·</td><td class="dead">·</td>',
            '<td class="mono">BT-2025-0001234</td><td class="mono">ARI-2025-0013456</td><td class="mono">1200.00</td><td class="dead">·</td><td class="mono live">FX_DIFFERENCE</td>',
            '<td class="mono">BT-2025-0001240</td><td class="dead">·</td><td class="dead">·</td><td class="mono">BANK_FEES</td><td class="dead">·</td>',
        ],
    )
    components = [
        ("allocation F1", schema.W_ALLOCATION, m.get("allocation_f1", 0.0), 25.0),
        ("transaction accuracy", schema.W_TRANSACTION, m.get("txn_accuracy", 0.0), 30.0),
        ("exception macro F1", schema.W_EXCEPTION, m.get("exception_macro_f1", 0.0), 20.0),
    ]
    rows = []
    for label, weight, value, scale in components:
        points = 100 * weight * value
        soft = label.startswith("exception")
        rows.append(
            f'<td class="mono">{label}</td>'
            f'<td class="mono">{weight:.2f} × {value:.4f}</td>'
            f'<td class="n">{points:.2f}</td>'
            f'<td class="cell">{bar(points, scale, soft)}</td>'
        )
    score_table = table(
        f"score {facts.score:.2f} = 100 × (0.50 F1 + 0.30 accuracy + 0.20 macro F1)",
        ["component", "weight × value", "points", "share of its own maximum"],
        rows,
    )
    emitted = len(schema.EXCEPTIONS) - len(facts.dead_codes)
    return f"""<section class="sec" id="score">
  <div class="wrap">
{section_head(stage, 6)}
    <div class="sec-grid">
{notes(
    note("inv", "Every bank line appears exactly once, as item rows or as a single category row, and never as both. No duplicate <code>(bank_txn_id, item_id)</code>. <code>make validate</code> checks the same rules the server does, before any upload."),
    note("info", f"The file is scored offline with the vendored scorer, so this number is the leaderboard's number, and the {len(facts.segments)} segments below are the same slices it reports."),
    note("info", f"The exception weight is worth {100 * schema.W_EXCEPTION * m.get('exception_macro_f1', 0):.2f} points because only {emitted} of {len(schema.EXCEPTIONS)} codes is ever emitted. Macro F1 averages over classes, not rows, so one rare code is worth as much as a segment with hundreds of lines."),
    note("info", "<code>process --trace</code> runs a <code>DecisionHook</code> after the matcher has already decided (it observes, it cannot settle an item — that would break the chronological book in the index stage) and writes which rule fired and how many candidates it had per line. <code>evaluate --explain</code> reads that sidecar and the golden file to turn the score into a report: which failure kind lost the most lines, and which rule to fix next."),
)}
{score_table}
{shapes}
    </div>
  </div>
</section>"""


def stage_ship(facts: Facts) -> str:
    stage = STAGES[6]
    if not facts.ready or facts.submission is None:
        body = f"""    <div class="note note-info"><dt>note</dt><dd>{e(facts.error) or "Run the phase once and the transcript appears here."}</dd></div>"""
        return f"""<section class="sec" id="ship">
  <div class="wrap">
{section_head(stage, 7)}
{body}
  </div>
</section>"""
    rel = facts.submission.relative_to(ROOT)
    info = describe(facts.submission)
    term = f"""      <div class="term">
        <div class="term-top"><b>real output</b><span>{e(facts.phase)} phase</span></div>
        <pre><span class="p">$</span> make process PHASE={e(facts.phase)}
<span class="o">[{e(facts.phase)}] {n(facts.lines)} bank lines, {n(facts.items)} items -&gt; {e(rel)}</span>
<span class="o">[{e(facts.phase)}] {n(facts.lines)} lines -&gt; {n(facts.item_rows)} item rows + {n(facts.no_item_rows)} category rows</span>

<span class="p">$</span> make evaluate PHASE={e(facts.phase)}
<span class="o">score {facts.score:6.2f} | alloc F1 {facts.metrics.get('allocation_f1', 0):.4f} (P {facts.metrics.get('precision', 0):.4f} / R {facts.metrics.get('recall', 0):.4f})</span>
<span class="o">{n(facts.rows)} rows over {n(facts.metrics.get('txns_covered', 0))}/{n(facts.lines)} lines</span>

<span class="p">$</span> python -m bankrec whoami
<span class="o">{{"team": "...", "status": "..."}}   # GET /api/me, uses no upload slot</span>

<span class="p">$</span> make push PHASE=LIVE
<span class="o">-- about to spend a LIVE submission ------------------------------</span>
  phase            LIVE  (public leaderboard, repeatable)
  rows             {n(info['rows'])}
  uncompressed     {info['uncompressed_bytes'] / 1024:.0f} kB  (packed {info['packed_bytes'] / 1024:.0f} kB)
  sha256 (csv)     {info['sha256']}
<span class="p">Type 'LIVE' to upload, anything else aborts:</span>
<span class="o"># nothing was typed, so nothing was uploaded, and no slot was spent</span></pre>
      </div>"""
    return f"""<section class="sec" id="ship">
  <div class="wrap">
{section_head(stage, 7)}
    <div class="sec-grid flip">
{term}
{notes(
    note("inv", "No flag, no environment variable and no <code>--yes</code> skips the gate. Without a terminal it refuses before any HTTP call, so an agent or CI can build and validate but cannot spend the submission."),
    note("info", "The printed sha256 is of the decompressed CSV, because gzip embeds a timestamp: two identical files have different <code>.gz</code> hashes, so only the decompressed hash identifies what is being uploaded."),
    note("info", "FINAL is one scored submission per team, so its phrase is longer (<code>SUBMIT FINAL</code>) and the row count, size and limits are checked before the prompt, not after."),
)}
    </div>
  </div>
</section>"""


def stage_levers(facts: Facts) -> str:
    stage = {"title": "Where the missing points are", "src": "make score", "detail": f"per segment, {facts.phase} phase"}
    if not facts.segments:
        return f"""<section class="sec" id="levers">
  <div class="wrap">
{section_head(stage, 8)}
    <div class="note note-info"><dt>note</dt><dd>{e(facts.error) or "Run the phase once and the segment table appears here."}</dd></div>
  </div>
</section>"""
    ordered = sorted(facts.segments.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    rows = []
    for name, info in ordered[:18]:
        acc = info["accuracy"]
        need = SEGMENT_NEEDS.get(name, "") or "not mapped yet"
        cls = "live" if acc == 0 else ("acc" if acc >= 0.6 else "mono")
        rows.append(
            f'<td class="mono {cls}">{e(name)}</td>'
            f'<td class="n">{n(info["n"])}</td>'
            f'<td class="n">{acc:.3f}</td>'
            f'<td class="cell">{bar(info["n"], max(v["n"] for v in facts.segments.values()) * 1.0, acc >= 0.6)}</td>'
            f'<td class="wrap{" dead" if need == "not mapped yet" else ""}">{e(need)}</td>'
        )
    seg_table = table(
        f"segments by lines, first {len(rows)} of {len(facts.segments)}",
        ["segment", "lines", "accuracy", "lines (bar)", "what it needs"],
        rows,
    )
    zero = sum(1 for v in facts.segments.values() if v["accuracy"] == 0)
    lines_zero = sum(v["n"] for v in facts.segments.values() if v["accuracy"] == 0)
    return f"""<section class="sec" id="levers">
  <div class="wrap">
{section_head(stage, 8)}
    <div class="sec-grid">
{notes(
    note("info", f"{zero} of {len(facts.segments)} segments still score zero, and they hold {n(lines_zero)} of {n(facts.lines)} lines. Every one of them is a missing branch, not a tuning problem: precision is already {facts.metrics.get('precision', 0):.3f}."),
    note("info", "Add evidence, then measure. One branch per change, compared per segment, kept only if it moves the segment it targets without regressing the others."),
)}
{seg_table}
    </div>
  </div>
</section>"""


def footer(facts: Facts) -> str:
    return f"""<footer class="foot">
  <div class="foot-in">
    <div>
      <h2>generated, not written</h2>
      <p>
        <code>scripts/build_map.py</code> reads the closed lists from <code>schema.py</code>,
        checks that every stage still points at a real function, verifies the diagram geometry,
        and scores an existing submission for the numbers. Hand edits are overwritten:
        <code>make map</code> rewrites this file, <code>make map-check</code> fails when it is
        stale.
      </p>
    </div>
    <dl>
      <dt>numbers from</dt><dd><code>make process</code> + <code>make evaluate</code>, scored offline</dd>
      <dt>diagram</dt><dd><code>scripts/map_diagram.py</code></dd>
      <dt>styles</dt><dd><code>scripts/map_theme.css</code></dd>
      <dt>contract</dt><dd><code>src/bankrec/schema.py</code>, <code>docs/POLICIES.md</code></dd>
      <dt>decisions</dt><dd><code>CLAUDE.md</code></dd>
    </dl>
  </div>
</footer>"""


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
TRACE_JS = """<script>
(() => {
  // Hover a block: keep it and everything it reaches, in both directions. Dim the rest.
  const data = JSON.parse(document.getElementById('graph').textContent);
  const svg = document.querySelector('.fig svg');
  if (!svg) return;
  const out = new Map(), inc = new Map();
  const link = (map, a, b) => { if (!map.has(a)) map.set(a, new Set()); map.get(a).add(b); };
  data.edges.forEach(({ a, b }) => { link(out, a, b); link(inc, b, a); });
  link(out, data.loop[0], data.loop[1]);

  const nodes = new Map([...svg.querySelectorAll('.nd')].map(el => [el.dataset.node, el]));
  const edges = [...svg.querySelectorAll('.ed')];

  const walk = (map, start) => {
    const seen = new Set([start]); const stack = [start];
    while (stack.length) {
      for (const next of (map.get(stack.pop()) || [])) {
        if (!seen.has(next)) { seen.add(next); stack.push(next); }
      }
    }
    return seen;
  };

  const clear = () => {
    svg.classList.remove('trace');
    svg.querySelectorAll('.on').forEach(el => el.classList.remove('on'));
  };

  svg.addEventListener('pointerover', (event) => {
    const target = event.target.closest('.nd');
    if (!target) return;
    const id = target.dataset.node;
    const keep = new Set([...walk(out, id), ...walk(inc, id)]);
    clear();
    svg.classList.add('trace');
    for (const [node, el] of nodes) if (keep.has(node)) el.classList.add('on');
    for (const edge of edges) {
      const [a, b] = edge.dataset.edge.split('>');
      if (keep.has(a) && keep.has(b)) edge.classList.add('on');
    }
  });
  svg.addEventListener('pointerleave', clear);
  svg.addEventListener('focusin', (event) => {
    const target = event.target.closest('.nd');
    if (target) target.dispatchEvent(new PointerEvent('pointerover', { bubbles: true }));
  });
  svg.addEventListener('focusout', clear);
  addEventListener('keydown', (event) => { if (event.key === 'Escape') clear(); });
})();
</script>"""

RELOAD_JS = """<script>
(() => {
  let rev = null;
  const tick = async () => {
    try {
      const res = await fetch('/__rev', { cache: 'no-store' });
      const next = (await res.json()).rev;
      if (rev && next !== rev) location.reload();
      rev = next;
    } catch (_) { /* server gone */ }
    setTimeout(tick, 900);
  };
  tick();
})();
</script>"""


def render(facts: Facts, theme: str, reload: bool = False) -> str:
    body = "\n\n".join(
        [
            spec(facts),
            figure(facts),
            stage_load(facts),
            stage_index(facts),
            stage_candidates(facts),
            stage_allocate(facts),
            stage_explain(facts),
            stage_score(facts),
            stage_ship(facts),
            stage_levers(facts),
        ]
    )
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="generator" content="scripts/build_map.py">
<title>bankrec pipeline map</title>
<meta name="description" content="A generated technical sheet for the bank reconciliation pipeline: block diagram, stage-by-stage rules, closed lists, score composition and the per-segment gaps.">
<style>
{theme.rstrip()}
</style>
</head>
<body>
<a class="skip" href="#top">Skip to the sheet</a>

<main id="top">
<div class="sheet">
{header(facts)}
</div>
{nav()}
<div class="sheet">
{body}
</div>
{footer(facts)}
</main>

{TRACE_JS}
{RELOAD_JS if reload else ''}</body>
</html>
"""


def build(phase: str, submission: Path | None = None, reload: bool = False) -> tuple[str, Facts]:
    verify_stages()
    map_diagram.check()
    theme = THEME.read_text(encoding="utf-8")
    facts = collect(phase, submission)
    return render(facts, theme, reload=reload), facts


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------
class _State:
    def __init__(self, phase: str, submission: Path | None):
        self.phase = phase
        self.submission = submission
        self.page = ""
        self.rev = 0
        self.lock = threading.Lock()
        self.mtimes: dict[Path, float] = {}

    def watched(self) -> list[Path]:
        return [
            THEME,
            Path(__file__),
            ROOT / "scripts" / "map_diagram.py",
            ROOT / "scripts" / "map_svg.py",
            *sorted((ROOT / "src" / "bankrec").glob("*.py")),
        ]

    def rebuild(self) -> None:
        try:
            page, _ = build(self.phase, self.submission, reload=True)
        except SystemExit as exc:
            page = f"<pre>{e(exc)}</pre>"
        except Exception as exc:  # a broken pipeline should show up in the browser
            page = f"<pre>{e(type(exc).__name__)}: {e(exc)}</pre>"
        with self.lock:
            self.page = page
            self.rev += 1
            self.mtimes = {p: p.stat().st_mtime for p in self.watched() if p.exists()}

    def changed(self) -> bool:
        return any(not p.exists() or self.mtimes.get(p) != p.stat().st_mtime for p in self.watched())


def serve(phase: str, port: int, submission: Path | None) -> int:
    state = _State(phase, submission)
    started = time.time()
    state.rebuild()
    print(f"built in {time.time() - started:.1f}s, serving http://localhost:{port}/", file=sys.stderr)

    def watch() -> None:
        while True:
            time.sleep(0.7)
            if state.changed():
                time.sleep(0.15)
                state.rebuild()
                print(f"rebuilt (rev {state.rev})", file=sys.stderr)

    threading.Thread(target=watch, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/__rev"):
                with state.lock:
                    body = f'{{"rev": {state.rev}}}'.encode()
                ctype = "application/json"
            else:
                with state.lock:
                    body = state.page.encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rebuild pipeline-map.html from the code")
    parser.add_argument("--phase", default="sample", help="phase whose submission is scored")
    parser.add_argument("--submission", type=Path, help="score this file instead")
    parser.add_argument("--check", action="store_true", help="exit 1 if pipeline-map.html is stale")
    parser.add_argument("--serve", action="store_true", help="serve with live reload")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    submission = args.submission
    if submission is not None and not submission.is_absolute():
        submission = ROOT / submission

    if args.serve:
        return serve(args.phase, args.port, submission)

    started = time.time()
    page, facts = build(args.phase, submission)
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != page:
            print(f"{OUT.name} is stale: run `make map`", file=sys.stderr)
            return 1
        print(f"{OUT.name} is up to date ({len(page):,} bytes)", file=sys.stderr)
        return 0

    OUT.write_text(page, encoding="utf-8")
    state = "with numbers" if facts.ready else f"without numbers ({facts.error})"
    print(f"wrote {OUT.name} in {time.time() - started:.1f}s, {state}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
