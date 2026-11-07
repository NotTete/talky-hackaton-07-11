# Handoff — continue this repo with Claude Code

You are continuing work in `/home/tete/Documents/Talky/talky-hackaton-07-11`
(Talky × Anthropic "Close the Books" bank-reconciliation hackathon, CPU only).

Read `CLAUDE.md` (decision log: why, not what) and `README.md` (how to run) before
changing anything. This file is the *current state* and the *next steps*; those two are
the contract.

---

## 1. What the product is

Explain every bank line of a synthetic multinational, submit a CSV, get scored by a fixed
formula:

```
score = 100 × (0.50 × allocation F1 + 0.30 × transaction accuracy + 0.20 × exception macro-F1)
```

- `sample` and `train` have golden labels → local score. `live` and `final` do not.
- LIVE is public and repeatable; FINAL is **one shot**.

## 2. Commands you will use (exact)

```bash
# venv: .devenv/state/venv/bin/python  (devenv + uv).  Never use system python.
cd /home/tete/Documents/Talky/talky-hackaton-07-11
export PYTHONPATH=src

./.devenv/state/venv/bin/python -m bankrec doctor
./.devenv/state/venv/bin/python -m bankrec process --phase sample --name my-change
./.devenv/state/venv/bin/python -m bankrec evaluate          # scores the newest build
./.devenv/state/venv/bin/python -m pytest -q                 # ~2:40, 37 tests
./.devenv/state/venv/bin/python -m bankrec pdf-facts --phase sample --record
./.devenv/state/venv/bin/python -m bankrec pdf-facts --explain data/sample/pdfs/ES01/ARI_FV250110868.pdf
./.devenv/state/venv/bin/python -m bankrec builds
```

`make` wrappers exist (`make process PHASE=sample NAME=x PDF_FACTS=1`, `make pdf-facts`).

**Hard rule: an agent never uploads.** `submit()` requires a human to type the phase on a
real TTY; there is no `--yes`/`--force` and adding one breaks `tests/test_submit_guard.py`.
Build, evaluate, validate freely; hand `make push PHASE=LIVE` to the human.

## 3. Where the code is

```
src/bankrec/
  alt/            the one engine (operation-first). The old baseline and advanced/ were
                  removed; they live in git history.
    engine.py     class Engine(:87), _resolve_doc(:428); PDF hook at :448
    ops.py advice.py refs.py ledger.py excepts.py cats.py
  preprocess.py   batch/card/cheque/payment-run/advice context (generation only)
  documents.py    preprocessing report  (NOT yet PDF-aware)
  stages.py       stage run registry: record(stage, phase, note=, metrics=, reports=)
  insights.py builds.py evaluate.py submit.py cli.py pipeline.py hooks.py
  pdf/            NEW — see §4
```

Sample score of the current code: **74.31**. Control: `make ratchet` (hash + score + 65
segment floors in `tests/fixtures/`); accept an intended change with `make ratchet-update`.


## 4. The PDF layer (what was just built, and what it is not)

`src/bankrec/pdf/` turns a PDF into **facts, never decisions**:

| file | job | entry points |
|---|---|---|
| `read.py` | positions → lines → bands → label/value pairs | `pairs(pages)` :203 |
| `fields.py` | money/date/percent normalisation, label hints | `money` :64, `date` :85, `resolve` :161 |
| `clauses.py` | clause anchors per code + params (document, pct, amount, count, direction, month, iban) | `extract(text)` :104, `replacement(clauses)` :157 |
| `facts.py` | `DocFacts`, `PdfFacts` index, JSONL cache | `for_docno`/`replacement_for`/`for_item`/`clauses_for_item`, `load`/`save` |
| `build.py` | parallel build, sha-keyed cache reuse, report + meta json | `build_phase` :89, `report` :148 |

Clause codes already extracted: `instalments`, `retention`, `advance`, `replaces`,
`iban_change`, `assignment`, `credit_note` (+ `direction: offset|refund`), `rappel`,
`service_month`.

Measured coverage:

| phase | docs | build | document no. | total | clauses |
|---|---|---|---|---|---|
| sample | 2,298 | 2.4 s | 94.6% | 94.6% | instalments 231, replaces 200, retention 86, assignment 407, advance 12, credit_note 61 |
| live | 16,688 | 22 s | 96.1% | 94.5% | instalments 1,308, replaces 1,009, credit_note 698, retention 155, service_month 102, iban_change 57 |

Integration points that exist today:

- `cli.py:237 cmd_pdf_facts`, parser at `:517`; `process` has `--pdf-facts`;
- `pipeline.py:51` takes `pdf_facts=`, loads it at `:68` when `settings.use_pdf_facts`;
- `Engine.__init__(..., pdf=None)` (`engine.py:92`), used in exactly **one** place:
  the `replaces` fallback at `engine.py:448`;
- `config.Settings.use_pdf_facts` (default **False**), `config/settings.toml`;
- tests: `tests/test_pdf_facts.py` (6 tests).

**Integrated and on by default.** Consumed: `replaces`, credit-note `offset` veto, final
instalment, retention release (74.07 → 74.31). Unused: advance, service month, assignment.

## 5. Next steps (ceilings were measured on 73.60; re-measure)

| # | what | ceiling | notes |
|---|---|---|---|
| 1 | credit-note discovery in `ar_1toN_credit_note` (199 lines, acc 0.37): the netted ARC is not found | high | candidate generation, not PDF; PDF has no clause on customer notes |
| 2 | advance (proforma) deduction; instalments whose earlier payments never matched | **+0.3** | `clauses[].count`, `pct`, `amount`, `amounts` parsed; done: final instalment, retention release, supplier refund |
| 3 | service month: month-aware tie-break for equal monthly fees | **+0.31** | `clauses[].month` as `YYYY-MM` |
| 4 | assignment / confirming | **+0.22** | weakest; skip unless cheap |

Do them one at a time: `make ratchet` → keep only if the score or a named segment improves
and none drops; `bankrec compare <old-build> <new-build>` shows which segment moved
(`bankrec evaluate <build> --explain` for individual lines). `make ratchet-update` in the
same commit when the output is meant to move.

Housekeeping that carries no score risk:

- `pdf/build.py:115` reuses a cached fact when the **file sha** matches only. Add an
  `EXTRACTOR_VERSION` constant to the key so a parser change rebuilds automatically
  (`--rebuild` is the current manual workaround).
- `build_phase` reports `documents` as the number of **index rows**. On `train-lite` that
  prints 131,637 while only 33,204 files exist (dangling index rows). Count parsed docs.
- `documents.py` / `bankrec documents` does not mention PDFs. Add a short section: docs by
  type, field coverage, clause counts, flag counts — a broken extraction must be visible.
- Surface fact flags (`no_text`, `no_docno`, `arithmetic_fail`, `sparse`) in
  `evaluate --explain` so a low-confidence fact can be reviewed instead of silently used.

## 6. Traps that already cost time (do not re-learn)

1. `Nº` folds to `no` (ordinal indicator has a compatibility decomposition) — doc-no hints
   must include `no`/`no.`. Without it every document number is lost.
2. `1,752.08` must parse as 175208, not 1.75. Both decimal conventions are live
   (ES/FR/DE and EN). Fixing this took LIVE total coverage 57% → 94.5%.
3. Money is **integer cents everywhere**; conversion happens once at the IO boundary.
   A float amount in a code path is a bug.
4. Closed lists (`category`, `exception`) live in `schema.py`; an unknown value rejects the
   whole file server-side.
5. Determinism: no unsorted `set` iteration into the output; gzip `mtime=0`. Compare builds
   with `gzip -dc file | sha256sum`.
6. Never edit `vendor/bankrec_score.py` or `docs/` (organisers' code and spec).
7. Never load TRAIN items for LIVE/FINAL (`open_items_at_start` is the carry-over).
8. A false negative in candidate generation is unrecoverable — when blocking, protect recall.

## 7. Definition of done for the next increment

- `make score` improved (or a named segment did, with no regression elsewhere);
- `bankrec validate` still passes, `pytest` still passes;
- the change is explainable by one sentence in `docs/POLICIES.md`;
- README still describes the code.

Start by running `bankrec doctor`, `pytest -q` and `bankrec builds`; then pick item 1 in §5.
