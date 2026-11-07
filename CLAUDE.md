# CLAUDE.md — why this repo is shaped this way

Context for anyone (human or agent) changing this code. The *what* is in
[`README.md`](README.md) and [`docs/`](docs/); this file is the decision log, so you
don't re-litigate choices that were already paid for.

> **Picking up work in progress?** Read [`HANDOFF.md`](HANDOFF.md) first: current
> numbers, the exact commands, where the code is, and the next measured increments.

> One-line brief: explain **every** bank line of a synthetic multinational, submit a
> CSV, be scored by a fixed formula. The code exists to make one thing cheap:
> *turning evidence into a testable hypothesis*.

---

## 1. Why the effort goes where it goes

The score is a fixed weighted sum:

```
0.50 × allocation F1 + 0.30 × transaction accuracy + 0.20 × exception macro-F1
```

Consequences that should drive every change:

- **Recall is the expensive half of F1.** Precision is already ~0.76 with a dumb
  matcher; recall is ~0.10. Adding a *correct* branch to a 25-item payment moves F1
  far more than tightening precision, because F1's denominator has both `TP+FP` and
  `TP+FN`.
- **Transaction accuracy is all-or-nothing.** One missing item loses the line even if
  the other 24 are right. So "partial credit" does not exist inside a line: when you can
  see the full document set (advice, batch file), use **all of it**.
- **Macro-F1 averages over classes, not rows.** `SHORT_WITHHOLDING` (8 cases in the
  sample) is worth as much as `PARTIAL` (466). Cheap exception rules on rare codes are
  the best points-per-line-of-code in the project.
- Therefore: **breadth of patterns beats depth of tuning.** One branch per segment,
  measured, is the intended workflow.

## 2. Why the engine only emits a pair on evidence

A wrong pair can only hurt (transaction accuracy) while a right pair helps twice (F1 and
accuracy), so every resolver emits a pair only on evidence: a document that lists it, an
exact reference, the right party and an amount that adds up, a globally unique amount.
Do not "improve" recall by loosening tolerances; add evidence instead (a new resolver, a
PDF fact). The control that tells you whether it worked is the ratchet (§9b), not an old
engine.

## 3. Why money is integer cents

The scorer accepts an amount when `|submitted − golden| ≤ max(0.01, 0.5 %)`. Floats
introduce exactly the kind of half-cent drift that flips that comparison near the
boundary, and it silently accumulates across 3 M rows. Conversion happens **once**, at
the IO boundary (`data.to_cents`); everything downstream is `int`. If you find a float
amount in a code path, that is a bug.

## 4. Why the closed lists are code, not configuration

`exception`/`category` are closed lists and unknown values **reject the whole file** on
the server. Keeping them in `schema.py` next to the tolerance and the score weights
means an agent cannot invent `SHORT_FEE` because it felt natural: the constants are the
contract, and `make validate` fails fast. Same reason the vendored scorer is the source
of truth for tolerance: one definition, no drift.

## 5. Why matching is a pipeline of ranked evidence, not one clever rule

Every real-world pattern (1:1 transfer, 1:N advice, batch, credit-note netting, FIFO
monthly fee) is *the same question* — "which open items does this line settle?" — asked
with different evidence. A ranked candidate list lets one allocator serve all of them:
the `Ledger` produces candidates, the allocator (`_rows_from_docs`, `_share`) picks the subset whose sum explains the cash.
This is why the hard parts are additions to candidate generation rather than new
top-level branches. Keep that separation: **generation asks "could it?", allocation asks
"does it add up?"**.

## 6. Why candidate generation runs per line instead of a global join

A global join (references × items) would be faster on paper but destroys the ability to
reason about *one* line at a time, which is exactly how the policy is written
("`PARTIAL` if the item is still open **after this line**"). It also makes the temporal
book possible (see §7). The cost is a Python loop over ~1.5 M lines; that is acceptable
because the candidate set per line is tiny — and because **a false negative in candidate
generation is unrecoverable** (no later stage can match what was never proposed). When
you add blocking, optimise for candidate recall first, precision second.

## 7. Why the item book is stateful and chronological

`PARTIAL`, `DUPLICATE_PAYMENT` and `RETURNED` are not properties of a row; they are
properties of *the state of the item at that moment*. So items are processed in
booking-date order and balances are decremented as we go, which means:

- an item paid twice is only detectable because the first payment closed it;
- a returned direct debit re-opens an item that was closed earlier;
- "same amount, several open invoices → oldest first" needs the set of *open* items,
  not the set of all items.

Any change that batches lines out of order (e.g. vectorising allocation globally) has to
reintroduce this state or it will silently lose those three exception classes.

## 8. Why FX is compared in EUR

`amount` must be in the **item** currency, but the cash is in the **account** currency.
Comparing them directly makes every cross-currency line look like a rounding error.
Converting both sides to EUR (`FXTable`/`FX`) keeps one comparison scale, and lets the
`FX_DIFFERENCE` rule be a simple "currencies differ + item fully cleared" test instead of
a per-pair spread calculation. Note the deliberate tolerance asymmetry: the *policy*
allows bank spreads up to 3 %, but a submitted amount still has to be within 0.5 % of the
*golden* amount — those are two different comparisons, don't merge them.

## 9. Why there is one engine (and how it is kept honest)

There used to be two: a conservative baseline (`matching.py` + `rules.py`, 39.56 on the
sample) kept as a control, and `bankrec.alt`. The baseline and the `advanced/` reference
implementation were removed once `alt` (73 -> 74 on the sample) had absorbed everything
they knew. They stay in git history; the commit before "single engine" is the place to
read them. Keeping a second code path costs every change two edits, and the baseline had
stopped being a useful comparison: it could not move.

The control is now mechanical (§9b): `make ratchet` rebuilds the sample, compares the
decompressed output hash and the score and per-segment accuracy against committed
fixtures, and fails on a drop. "Alt minus baseline on the same phase" became "this
commit minus the previous floor".

## 9b. How the engine thinks, and the regression ratchet

`bankrec.alt` asks **what kind of operation is this line** — a direct-debit batch credit,
a card settlement, a confirming remittance at maturity, a returned collection, a plain
transfer — and only then looks for items. For the aggregate segments there is nothing
to rank: `direct_debit_batches/`, `card_settlements/`, `cheque_deposits/`,
`payment_runs/` and the remittance advices *contain the answer*, and the only real
question is which file row belongs to which bank line. The rest is evidence ranked by
arithmetic (§5).

**The ratchet.** `scripts/ratchet.py` (`make ratchet`, `make ratchet-update`,
`tests/test_regression_sample.py`) builds the sample with default settings and compares
against `tests/fixtures/`: the sha256 of the *decompressed* CSV (§12), the score, and 65
per-segment accuracies. A pure refactor must leave the hash identical. A rule meant to
move the output runs `make ratchet-update` in the same commit and says what moved. Today
it also caught a real bug: a set of credit notes iterated into the output made two
`PYTHONHASHSEED`s produce different files.

**PDF facts are on by default** (`use_pdf_facts = true`; `process` builds the cache when
it is missing). They enter only as evidence (§15c): a replacement clause, a credit-note
`offset` veto, the last instalment of a plan, a retention release.

Things inside the engine worth knowing before changing it:

* **An operation only answers for a line whose amount it can account for**
  (`ops.select`). A batch id appears in the collection line's text *and* in its fee line
  ("COMISION REMESA 03250603661 125 RECIBOS", -18.75 against an 89,776.76 batch).
  Expanding the batch for the fee line added 125 phantom rows and marked every receipt a
  duplicate. Direction is checked the same way: a payment run never explains a receipt.
* **The inference resolvers are arbitrated by arithmetic, not by order.** A truncated
  reference that resolves to one 1,265.84 invoice must not beat the 42-invoice sweep that
  sums to the 100,423.72 on the statement, so `resolve()` prefers the proposal whose
  amounts net to the cash and falls back to evidence order only for ties.
* **Document numbers are matched by prefix *and* suffix.** The bank drops our prefix far
  more often than it truncates the number ("FRAS 0414882" for FV25-0414882), and the
  dropped part is not a fixed string. `ledger` answers suffix queries with one bisect
  over a reversed copy of the key list, which is a list of strings rather than the
  3-million-entry tail index the obvious version needs (§11).
* **A lone amount coincidence never pays a credit note.** A -19.21 bank fee is not the
  settlement of a -19.21 supplier credit note; a receipt equal to one open supplier credit
  note from a known payer is a `_supplier_refund`, and only as a last resort.
* **Credit notes are netted per party, and only when the invoices alone do not already
  explain the cash** with three invoices or fewer; one invoice plus one note is too easy
  to hit by coincidence (it cost 21 correct lines in `ar_ref_noise` when ungated).

## 10. Why polars for IO and Python for matching

Amdahl, not taste. Reading/filtering/joining 1.5 M rows is bulk work where a
multi-threaded Arrow engine wins by 10–100×; matching is a per-line loop where the
candidate set is a handful of rows and the cost is Python dict lookups. Switching the
*matcher* to polars would buy nothing and cost clarity. Switching the *loaders* to
pandas costs seconds on TRAIN. If you vectorise a stage, do it where the row counts are
large — and keep the boundary explicit (`items()` returns a DataFrame, `Ledger`
converts to dict rows once).

## 11. Why every master-derived lookup is precomputed

Measured, not hypothetical: an innocent `@property` that re-normalised 62 k IBANs was
called once per bank line — 20 M string normalisations for 20 lines, minutes per phase,
and it looked like "polars is slow". `Masters` now builds `iban_owner`, `terms`,
`own_accounts` and `party_names` exactly once in `__init__`. Generalise the lesson: in the
per-line path, **anything O(masters) must be built once, and anything O(items) must be
indexed**. That single rule is the difference between 5 seconds and 30 minutes.

## 11b. Why LIVE costs ~20× the sample (and what NOT to do about it)

LIVE is 203 k lines against 1 M open items; the sample is 8.5 k against 66 k. That is
~24× the loop and ~15× the index, so ~89 s versus ~5 s is the honest baseline, not a bug.

Three things made it 815 s in practice, and none of them was "Python is slow":

1. **Loading TRAIN items for LIVE.** `open_items_at_start.csv.gz` *is* the carry-over
   balance; adding TRAIN marks years of settled invoices as open — 6.35 M items, a 48 s
   index, and worse candidates. `Phase.items()` now refuses the extra tables and explains
   why. Correctness fixes performance here, which is the good kind.
2. **Name votes over common tokens.** Summing votes over every token expanded a single
   common word ("POOL") into thousands of parties: 5.2 M comparisons per 3 k lines.
   Selective-token blocking (rarest token first, then intersect) and amount-first
   ordering cut it ~4×.
3. **Unbounded tolerance windows.** The 0.5 % tolerance means big amounts have wide
   windows; ranking thousands of equally plausible items is wasted work *and* hurts
   precision. Returning `[]` above 250 candidates is both faster and more honest.

What was deliberately **not** done: vectorising the whole matcher into polars joins. It
would win, but it would move allocation out of booking-date order, and that order is what
makes `PARTIAL` / `DUPLICATE_PAYMENT` / `RETURNED` possible (§7). Optimise the blocking,
keep the loop.

## 12. Why the output must be deterministic

A score you cannot reproduce is a score you cannot compare, and "it went up 0.4" is the
only signal this project has. Two traps found the hard way:

- **Set iteration is hash-seed dependent.** Ordering rows by iterating `ref_variants(...)`
  (a `set`) changed the row order between runs, which changed tie-breaks downstream.
  Everything that feeds the output is now sorted (`reference_tokens → sorted`,
  `ref_variants → sorted`, amount buckets → sorted, name-vote ties → `(-hits, id)`).
- **gzip embeds a timestamp**, so compressed hashes differ even when content is identical.
  Compare the *decompressed* output (`gzip -dc … | sha256sum`).

If you add a data structure that is iterated into the output, make its order explicit.

**A run must also never corrupt the output.** Two agents (or one agent and one human)
running `run --phase live` concurrently used to write the same file: the result was a
gzip stream with hundreds of KB of trailing zeros, which the scorer rejects and which
`gzip -t` only warns about. Hence the per-pid temp file + `os.replace`, plus a lock file
that makes a second run fail in milliseconds instead of after an hour of matching.

## 13. Why the scorer and the docs are vendored

`vendor/bankrec_score.py` is the server's own code and `docs/` is the organisers' spec.
Copying them in means (a) scoring works offline in 5 seconds instead of a round-trip per
experiment, (b) you cannot accidentally optimise against a reimplementation that drifted,
and (c) every rule in `alt/excepts.py` can be checked against the text it claims to implement.
The cost is that these files are third-party: **never edit them** — if you need different
behaviour, change the pipeline.

## 14. Why there is no "local leaderboard" for LIVE/FINAL

Golden labels exist only for TRAIN/sample, and the rules forbid obtaining LIVE/FINAL
answers by other means. So the only honest self-evaluation on unlabelled data is
**coverage and consistency**: how many lines you explain, how many rows per line, and
whether your own validation passes. That is also why `make score` refuses to run without
a golden file instead of silently reporting a misleading number.

## 15. Why the FINAL phase forces a different behaviour

There is exactly one scored FINAL submission; a file **rejected by validation does not
consume it**. That asymmetry is why `validate` exists as a separate command and why the
README insists on validating the same file format on LIVE first. Any automation that
uploads should validate locally, because the cheapest failure mode is the one that never
reaches the server.

### 15.1 The human gate on uploading (non-negotiable)

**A human always confirms before anything is uploaded. Always. No exceptions, and no
escape hatch.** `bankrec/submit.py` prints the phase, the file, the row count and the
sha256 of the *decompressed* CSV, then waits for the operator to type `LIVE` — or
`SUBMIT FINAL` for the single FINAL shot — and aborts on anything else.

Concretely, this is a rule for agents working in this repo:

- An agent may build and evaluate freely: `make process PHASE=…`, `make evaluate`,
  `make validate …`. Those are reversible and cost nothing but time.
- **An agent must never submit.** There is no `--yes`/`--force` flag and no environment
  variable that bypasses the prompt; if you add one you have broken the guarantee. With
  no TTY (pi's `bash` tool, CI, cron, a Jupyter cell) `submit()` raises `SystemExit`
  *before* any HTTP call, which is the intended outcome, not a bug to work around.
- The correct behaviour is to stop and hand the command to the person who owns the
  token: `make push PHASE=LIVE` (prints the build + warnings → prompt → upload) or
  `make push PHASE=FINAL BUILD=<id>`. Show them the numbers; let them type the phrase.
- **A 401 is always the token, never the code.** `whoami()` calls `GET /api/me`, the
  platform's "am I authenticated" endpoint, and `team_token()` strips the quotes and the
  `Bearer ` prefix a paste usually carries. Requesting an upload URL already consumes a LIVE
  slot, so `/me` is the only safe way to check one: `make whoami`. `.env` is read at import
  (`load_dotenv`), because `.env.example` has claimed that since the skeleton was written.
- `tests/test_submit_guard.py` pins this: if the gate ever stops firing without a
  terminal, a test fails before a scored submission can be lost, and another test fails if
  anyone re-adds a `--yes`/`--force` flag to the CLI.

Why so strict: an upload is irreversible, LIVE results are public, and FINAL is one
shot. The cost of an accidental submit is unbounded while the cost of one extra typed
word is four characters — the asymmetry decides it (§1).

## 15b. Why the workflow is `process → evaluate → push` (and why builds are named)

The three steps carry three different risks, so they are three commands, not one:

* **process** is cheap and repeatable. It is allowed to be wrong.
* **evaluate** is where wrongness is discovered. It writes the verdict into the registry.
* **push** spends something irreversible: a LIVE slot, or the *single* FINAL shot.

The failure mode this prevents is not hypothetical: "push whatever is in `submissions/`
right now" loses the answers to the only questions that matter at that moment — *which*
build is this, was it evaluated, is it the newest, was it already sent? So every build is
an immutable artifact with a content hash in its name:

```
builds/20261107-1044-35d1df3b-baseline.csv.gz
```

The hash identifies the content, the stamp records when it was built. The same CSV rebuilt
later gets a new stamp and the *same hash*, so "identical content already pushed" is a
detectable fact rather than a memory, and a re-run cannot inflate the build history. The hash is over the **decompressed** CSV because
gzip embeds a timestamp (§12) — hashing the `.gz` would make two identical submissions look
different.

`push` therefore reads the registry and warns: newer build exists / never evaluated /
failed validation / content already pushed / FINAL is one shot. It never guesses the file.
The warnings are printed *before* the typed confirmation, because the only moment they can
change a decision is before the shot is spent.

## 15c. Why PDF facts are evidence and not decisions

The reconciliation is CSV + ledger state; a PDF can only add what no CSV column carries.
So `src/bankrec/pdf/` produces facts (fields, clauses, checks, flags) and **nothing else**:
`build.py` writes a sha-keyed cache, `stages.record` records the run, and the engine reads
them only when `--pdf-facts` is passed. That is what makes a bad clause harmless: it is a
flag on a fact, not a branch in the matcher, and it can be switched off without changing
the build. It also keeps the honest measurement: wiring `replaces` moved the sample score
by 0.00 (73.60 -> 73.60), which is a real result and not something a "smarter" parser story
should hide. The two field bugs worth remembering are pinned in `tests/test_pdf_facts.py`:
`Nº` folds to `no` (the ordinal indicator has a compatibility decomposition) and
`1,752.08` must parse as 175208, not 1.75 — the second one moved LIVE total coverage from
57% to 94.5%.

## 16. Why data and builds are gitignored, tokens are not committed

The dumps are hundreds of MB and re-downloadable; builds are generated (and carry a hash,
so they are reproducible from the code). The team
token is a credential with a leaderboard side effect — it lives in the environment, never
in `settings.toml` or code. This is also why `submit.py` reads `HK_TOKEN` and fails loudly
when it is missing, instead of prompting or defaulting.

The download script keeps **archives and extracted phases apart** (`data/archives/` vs
`data/<phase>/`, plus `data/manifest.json`) because they answer different questions:
the extracted tree is what the pipeline reads, the archive is the proof of *what the
organisers actually published*. Keeping the archive is cheap relative to the
alternative — "which data produced this 31.4?" is unanswerable once it is deleted, and
one flaky Wi-Fi download becomes a 650 MB re-fetch. Hence: keep by default, opt out with
`--no-keep-archives`. Extraction is idempotent and recorded per archive (sha256 +
destination list) so a re-run repairs a half-unpacked phase instead of silently
scoring on it, and downloads use `.part` + Range so an interrupted 650 MB fetch resumes
rather than restarting. `train-lite` is a separate directory for the same reason it is a
separate archive: it is a subset of *raw files*, so scoring it as `train` would quietly
change what the number means.

## 17. Why the test asserts so little

`tests/test_smoke.py` checks that the sample phase produces a file the **official scorer
accepts** and scores above zero. It guards plumbing, not accuracy: accuracy is measured
continuously by `make score`, and encoding a baseline score in a test would turn every
intentional trade-off into a test failure. Add tests where a mistake is *invisible*
(amount formatting, row validation, determinism), not where the scorer already tells you.

## 18. Deliberate omissions (open work, with reasons)

| omitted | why it is not in the skeleton |
|---|---|
| PDF / XLSX advice parsing | needs an extractor dependency; `alt/advice.py` already reads PDF advices. CSV/TXT already cover most advice volume. |
| Bank code maps (`bank_code`/`bank_subcode`) | they are consistent per bank but undocumented; worth harvesting from TRAIN, not worth guessing. |
| Drift handling (N43 → camt.053, advice layout change, ZAR subsidiary) | these are date-range facts about the data; they belong in candidate generation once you have seen the LIVE text, not in the baseline. |
| ML ranking of candidates | the bottleneck is missing evidence (recall), not ranking quality. A model on top of a 10 %-recall candidate set learns the wrong thing. |
| Global optimisation (ILP over all lines) | violates the temporal book (§7) and the policy is local by construction. |
| Parquet caching of TRAIN | only matters once a run is slow; at that point measure first (§11). |

## 19. Invariants to preserve when you change something

1. **Every bank line appears** in the output (a line with no item still gets a category row).
2. **Amounts are positive integer cents** in the item currency, formatted with a dot.
3. **`category` is empty on item rows; `amount` is empty on no-item rows.**
4. **No duplicate `(bank_txn_id, item_id)`; no line mixes item rows and a category row.**
5. **Candidate generation never drops evidence** to make allocation simpler.
6. **The output is deterministic** for a fixed input.
7. **No LIVE/FINAL golden**, no scraping, no token in the repo.
8. **One change, one measurement**: `make score`, compare per-segment accuracy, keep only
   if it improves; TRAIN before LIVE, LIVE before FINAL.
9. **A human confirms every upload.** `submit()` asks the operator to type the phase on a
   real terminal and refuses without one; never add a bypass flag (§15.1).
10. **The map is generated.** Never hand-edit `pipeline-map.html`; change
   `scripts/build_map.py` (or the CSS) and run `make map` (§21).

## 20. How to tell an increment is done

Not "it looks right". It is done when:

- `make score` improved (or a specific segment did, without regressing others),
- `make validate` still passes,
- `pytest` still passes,
- the change is explainable by a sentence in `docs/POLICIES.md`,
- `make map` was re-run if you touched a stage (the page is checked in),
- and the README still describes the code (if you added a file, say why in the tree).

## 21. Why the pipeline map is generated, not written

`pipeline-map.html` is the one document whose whole job is to be *true*: seven stages, the
function behind each of them, and the numbers the scorer currently reports. A hand-written
version of that starts lying the first time someone edits `alt/engine.py`, and a map that lies
is worse than no map, because it is the first thing a new contributor (or an agent) reads.
So `scripts/build_map.py` produces it, and nothing on it is typed by hand:

| what the sheet says | where it comes from |
|---|---|
| closed lists, weights, limits, the tolerance | `import bankrec.schema` (no copy, no paraphrase of the values) |
| which codes are reachable, whether `PARTIAL` can fire | an AST scan of `rules.exception_for` |
| rows, score, per-segment accuracy, emitted codes | scoring an existing submission with the vendored scorer |
| the blocks, their flows, and the state loop | `scripts/map_diagram.py`, drawn from a node table and an edge table |
| which file implements each stage | `STAGES` in the generator, every symbol imported first |
| styling | `scripts/map_theme.css`, a real stylesheet, inlined at build time |

The page is a **schematic plus reference notes**, not a narrative: one block diagram
(`fig. 1`), then one short section per stage with the rule, the file, and the numbers.
Three consequences worth keeping:

- **A renamed function fails the build, not the docs.** `verify_stages()` imports every symbol
a stage claims; if `Engine._rows_from_docs` disappears the generator exits with the list of missing
references. That is the cheapest possible guard against documentation drift.
- **Missing data means no numbers, never zero numbers.** With no submission to score, the
page says so in the affected sections. It never prints `0.00` as if it had measured it
(a number you did not measure is a lie with a decimal point).
- **The diagram is routed, not drawn by eye.** Blocks sit on a 6x6 grid; `Route` picks a
channel inside each gutter (fewest crossings, never through a box, never sharing a lane with
another edge), and `check()` raises if two boxes overlap, if a label overflows its box, or if
text is longer than the box can hold. Editing the graph is a change to two lists, and the
build tells you when it got ugly. There is no JS graph library: a fixed grid plus a router is
smaller, deterministic, works offline, and renders without JavaScript.

`make map-check` compares the committed file with a fresh render and fails when they differ.
That is a *workflow* step after a scoring change, not a CI gate: the numbers move whenever
the pipeline or the data does, and a red build for that would train people to ignore it.
`pytest` keeps the data-free invariants instead (stages resolve, render is deterministic, no
em dash), so a broken generator is still caught on every run.

Serve mode (`make map-serve`) rebuilds on save and reloads the browser. It is deliberately
stdlib-only (`http.server` + a file-mtime watcher + a 900 ms `/__rev` poll) because the
value is the loop, not the tooling: edit a rule, look at the stage it belongs to, decide.

The only client JS is the trace: hovering (or tabbing to) a block keeps that block and
everything reachable from it, in both directions, and dims the rest. That is the cheapest
way to answer "what does this feed, and what feeds it" without a paragraph of prose.
