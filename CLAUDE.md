# CLAUDE.md — why this repo is shaped this way

Context for anyone (human or agent) changing this code. The *what* is in
[`README.md`](README.md) and [`docs/`](docs/); this file is the decision log, so you
don't re-litigate choices that were already paid for.

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

## 2. Why the baseline is deliberately conservative

`matching.py` only emits a pair on strong evidence (exact reference, or right party +
right amount, or a globally unique amount). Because a wrong pair can only hurt
(transaction accuracy) while a right pair helps twice (F1 and accuracy), the baseline
never guesses. Its job is to be **obviously correct and obviously incomplete**, so a new
branch is measurable against it. Do not "improve" it by loosening tolerances — add
evidence instead.

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
`ItemIndex` produces candidates, `Matcher` picks the subset whose sum explains the cash.
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

## 9. Why `advanced/` exists but is not imported

It is a fuller implementation (~31 on the sample) kept as **reference**, not as a
dependency: importing it would couple the skeleton to pandas-based code that predates the
polars data layer, and would make the "start small" claim false. The point of keeping it
is provenance — when a branch here looks arbitrary, the reason it matters is visible
there. Treat it as documentation you can read, not code you inherit.

## 10. Why polars for IO and Python for matching

Amdahl, not taste. Reading/filtering/joining 1.5 M rows is bulk work where a
multi-threaded Arrow engine wins by 10–100×; matching is a per-line loop where the
candidate set is a handful of rows and the cost is Python dict lookups. Switching the
*matcher* to polars would buy nothing and cost clarity. Switching the *loaders* to
pandas costs seconds on TRAIN. If you vectorise a stage, do it where the row counts are
large — and keep the boundary explicit (`items()` returns a DataFrame, `ItemIndex`
converts to dict rows once).

## 11. Why every master-derived lookup is precomputed

Measured, not hypothetical: an innocent `@property` that re-normalised 62 k IBANs was
called once per bank line — 20 M string normalisations for 20 lines, minutes per phase,
and it looked like "polars is slow". `Masters` now builds `iban_owner`, `terms`,
`own_accounts` and `party_names` exactly once in `__init__`. Generalise the lesson: in the
per-line path, **anything O(masters) must be built once, and anything O(items) must be
indexed**. That single rule is the difference between 5 seconds and 30 minutes.

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

## 13. Why the scorer and the docs are vendored

`vendor/bankrec_score.py` is the server's own code and `docs/` is the organisers' spec.
Copying them in means (a) scoring works offline in 5 seconds instead of a round-trip per
experiment, (b) you cannot accidentally optimise against a reimplementation that drifted,
and (c) every rule in `rules.py` can be checked against the text it claims to implement.
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

## 16. Why data and submissions are gitignored, tokens are not committed

The dumps are hundreds of MB and re-downloadable; submissions are generated. The team
token is a credential with a leaderboard side effect — it lives in the environment, never
in `settings.toml` or code. This is also why `submit.py` reads `HK_TOKEN` and fails loudly
when it is missing, instead of prompting or defaulting.

## 17. Why the test asserts so little

`tests/test_smoke.py` checks that the sample phase produces a file the **official scorer
accepts** and scores above zero. It guards plumbing, not accuracy: accuracy is measured
continuously by `make score`, and encoding a baseline score in a test would turn every
intentional trade-off into a test failure. Add tests where a mistake is *invisible*
(amount formatting, row validation, determinism), not where the scorer already tells you.

## 18. Deliberate omissions (open work, with reasons)

| omitted | why it is not in the skeleton |
|---|---|
| PDF / XLSX advice parsing | needs an extractor dependency; the `advices.py` hook in `advanced/` shows where it plugs in. CSV/TXT already cover most advice volume. |
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

## 20. How to tell an increment is done

Not "it looks right". It is done when:

- `make score` improved (or a specific segment did, without regressing others),
- `make validate` still passes,
- `pytest` still passes,
- the change is explainable by a sentence in `docs/POLICIES.md`,
- and the README still describes the code (if you added a file, say why in the tree).
