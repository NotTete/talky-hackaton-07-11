# Talky × Anthropic — *Close the Books*

**Bank reconciliation at scale.** For every bank line of a (fictitious) IBEX-35-sized
industrial group, decide which open items it settles and for how much, explain the
differences, and flag what cannot be identified.

This repo is a **small, working skeleton** with the plumbing already in place:

- it loads the official dumps in seconds,
- it produces a **valid** submission CSV (the official scorer accepts it),
- it **scores itself locally** with the same scorer the leaderboard uses,
- it has one obvious place to implement each hard part.

Baseline right now: `20.5` on the 1-month sample (the official starter scores ~10–15).
A fuller reference implementation that already handles remittance advices and batch
documents (~31 on the same sample) lives in `advanced/`.

The Python environment (devenv, uv, PyTorch, Hugging Face) is documented separately in
[`docs/DEVENV.md`](docs/DEVENV.md).

---

## 1. Quick start

```bash
./scripts/download_data.sh        # docs + masters + 1-month sample (~30 MB)
make doctor                       # check the layout
make sample                       # build submissions/submission_sample.csv.gz
make score                        # score it on the sample (official scorer)
```

Upload (needs your team token from the site → Console page):

```bash
read -rs HK_TOKEN && export HK_TOKEN
python -m bankrec validate submissions/submission_live.csv.gz --phase live   # pre-flight
python -m bankrec submit LIVE submissions/submission_live.csv.gz
```

No `make`? Everything is the same through the CLI:

```bash
PYTHONPATH=src python -m bankrec run     --phase sample
PYTHONPATH=src python -m bankrec score   submissions/submission_sample.csv.gz --phase sample
PYTHONPATH=src python -m bankrec doctor
```

> **Phases.** `sample` = one labelled month (fast loop). `train` = 4 labelled years.
> `live` = 2025-H2, scored on the public leaderboard as often as you like.
> `final` = 2026-H1, released near the end, **one scored submission per team**.
> Always validate on LIVE before spending the FINAL shot.

---

## 2. The task in one paragraph

The group has 12 currencies, 13 banks and ~50 accounts. A bank line can clear **one
invoice, 300 invoices**, or nothing at all (fees, payroll, cash-pool sweeps, tax…).
The hard part is not arithmetic, it is *evidence*: invoice numbers arrive truncated,
without prefix or with typos; a parent company pays for its subsidiaries; one SEPA
batch is a single bank line for 400 receipts; the reason for a deduction only exists
in a remittance advice PDF that may arrive days later.

You must explain **every** bank line. Lines you omit count as wrong.

---

## 3. Scoring (this is what to optimise)

```
score = 100 × (0.50 × allocation F1        # (bank line, item) pairs, amount within tol.
             + 0.30 × transaction accuracy # whole set of (item, amount, exception) per line
             + 0.20 × exception macro-F1)  # mean F1 over non-empty exception classes
```

- **Allocation F1** rewards *recall*: a 1:N payment where you emit 1 of 25 items scores
  terribly. Precision is easy (be conservative), recall is where the points are.
- **Transaction accuracy** requires the exact set — one missing item loses the line.
- **Exception macro-F1** is an *average over classes*, so `SHORT_WITHHOLDING` (8 cases)
  is worth as much as `PARTIAL` (466). Do not ignore the rare codes.
- Amount tolerance: `|submitted − golden| ≤ max(0.01, 0.5 % of golden)`.

Local scoring is the real thing — `evaluate.py` runs `vendor/bankrec_score.py`, the
file the server uses. Trust it, not your intuition.

---

## 4. Layout

```
./
├── README.md                  ← you are here
├── CLAUDE.md                  ← why the repo is shaped this way (decision log)
├── Makefile                   ← data / doctor / sample / score / train / submit
├── .env.example               ← HK_TOKEN etc. (never commit the token)
├── config/settings.toml       ← data path, phase, which item tables to load
├── docs/                      ← official README, POLICIES, FORMAT, DATA_DICTIONARY
│   ├── DEVENV.md              ← Python/uv/PyTorch environment notes
│   └── official-starter/      ← the organisers' starter + reference scorer
├── data/raw/                  ← download target (gitignored)
├── submissions/               ← generated CSVs (gitignored)
├── vendor/bankrec_score.py    ← official scorer, vendored so you can score offline
├── advanced/                  ← fuller reference implementation (~31), not imported
├── scripts/download_data.sh
├── notebooks/README.md        ← suggested first exploration cells
├── tests/test_smoke.py        ← sample → CSV → scored, end to end
└── src/bankrec/
    ├── __main__.py            ← `python -m bankrec …`
    ├── cli.py                 ← run | score | validate | submit | doctor
    ├── config.py              ← settings.toml + env overrides
    ├── schema.py              ← ⭐ the contract: columns, closed lists, tolerance
    ├── data.py                ← ⭐ polars loaders: dumps → canonical tables
    ├── text.py                ← ⭐ reference/name normalisation helpers
    ├── matching.py            ← ⭐⭐ IMPLEMENT HERE: what does a line settle?
    ├── rules.py               ← ⭐⭐ IMPLEMENT HERE: exception / category codes
    ├── pipeline.py            ← the loop: phase → submission CSV
    ├── evaluate.py            ← official scorer wrapper + per-segment report
    └── submit.py              ← presigned S3 upload + polling
```

`⭐` = read it before writing code. `⭐⭐` = the two files you will spend your day in.

---

## 5. The data (full detail in `docs/DATA_DICTIONARY.md`)

```
masters/    customers, suppliers, bank_accounts, payment_terms, fx_rates, categories
<phase>/    bank_transactions.csv.gz      ← the lines you must explain
            ar_invoices.csv.gz  ap_invoices.csv.gz  credit_notes.csv.gz
            open_items_at_start.csv.gz    (LIVE/FINAL only)
            golden_allocations.csv.gz     (TRAIN/sample only)
            bank_statements/              raw N43 / MT940 / camt.053 files
            direct_debit_batches/ card_settlements/ cheque_deposits/ payment_runs/
            remittance_advices/ pdfs/     ← 1:N and "why it differs" evidence
```

Columns you will use constantly:

| table | columns |
|---|---|
| `bank_transactions` | `bank_txn_id`, `account_id`, `entity`, `booking_date`, `amount` (signed, account currency), `currency`, `text`, `ref1`, `ref2`, `counterparty_name`, `counterparty_account`, `end_to_end_id`, `remittance_info`, `orig_amount`, `orig_currency` |
| AR/AP invoices | `item_id`, `document_number` (what the customer quotes), `issue_date`, `due_date`, `currency`, `net/tax/total_amount`, `payment_terms`, `customer_po` |
| credit notes | `item_id`, `side` (AR/AP), `document_number`, `total_amount`, `related_document`, `reason` |
| `open_items_at_start` | `item_id`, `item_type`, `party_id`, `document_number`, `currency`, `total_amount`, `open_amount` |
| `golden_allocations` | `bank_txn_id`, `item_id`, `amount`, `category`, `exception`, `segment`, `difficulty` |

**`segment` is your roadmap.** It tells you which pattern produced each row
(`ar_1toN_remittance`, `ap_partial`, `nonitem_bank_fees`, …). Sort segments by
`rows × (1 − your_coverage)` and work top-down.

---

## 6. The submission contract (full detail in `docs/FORMAT.md`)

One CSV, UTF-8, comma, header, optionally `.csv.gz`, **one row per allocation**:

```csv
bank_txn_id,item_id,amount,category,exception
BT-2025-0001234,ARI-2025-0456789,1210.00,,
BT-2025-0001234,ARI-2025-0456790,605.00,,
BT-2025-0001240,,,BANK_FEES,
BT-2025-0001300,ARI-2025-0400001,980.00,,SHORT_DISCOUNT
BT-2025-0001301,,,UNIDENTIFIED,UNIDENTIFIED
BT-2025-0001302,ARI-2025-0400002,400.00,,PARTIAL
```

| column | rule |
|---|---|
| `bank_txn_id` | a bank line of the phase you submit; omitted lines count as wrong |
| `item_id` | `ARI-`/`ARC-`/`API-`/`APC-` + year + 7 digits; empty when the line settles no item |
| `amount` | amount of the item cleared by this line, **in the item currency, positive**, dot decimals; empty when `item_id` is empty |
| `category` | only for no-item lines, from `masters/categories.csv` |
| `exception` | empty or one of the closed list below |

Limits: ≤ 60 MB uncompressed, ≤ 3,000,000 rows, max one no-item row per line, no
duplicate `(bank_txn_id, item_id)`, and a line cannot mix item rows and a category row.
`make validate` checks all of this before you upload.

---

## 7. Policy cheat sheet (`docs/POLICIES.md` has the exact wording)

**Exceptions**

| code | when | amount |
|---|---|---|
| *(empty)* | paid as documented, last instalment, credit notes netted | open amount |
| `PARTIAL` | item still open afterwards, no specific reason | cash applied |
| `SHORT_DISPUTE` | customer deducted with a stated reason (advice) | cash applied |
| `SHORT_DISCOUNT` | within terms + 3 days grace, cash = total − discount % | full open amount |
| `SHORT_BANK_CHARGES` | intermediary fee: shortfall ≤ 60 EUR **and** ≤ 2 % | full open amount |
| `SHORT_WITHHOLDING` | tax withheld: 1.25 / 1.5 / 10 % | full open amount |
| `OVERPAYMENT` | paid more than one invoice's open amount | full open amount |
| `FX_DIFFERENCE` | line currency ≠ item currency (replaces discount/charges/withholding) | full open amount |
| `DUPLICATE_PAYMENT` | item already settled, paid again | cash paid again |
| `RETURNED` | SEPA/cheque/card return re-opens the item | amount reversed |
| `UNIDENTIFIED` | receipt with no matching open item | — (with category `UNIDENTIFIED`) |

**Categories** (no-item lines): `BANK_FEES`, `INTEREST_INCOME`, `INTEREST_EXPENSE`,
`PAYROLL`, `SOCIAL_SECURITY`, `TAX_PAYMENT`, `TAX_REFUND`, `LOAN_DRAWDOWN`,
`LOAN_REPAYMENT`, `LEASING`, `FX_DEAL`, `CASH_POOL`, `INTERCOMPANY`,
`INTERNAL_TRANSFER`, `CUSTOMER_ADVANCE`, `CUSTOMER_REFUND`, `SUPPLIER_REFUND`,
`CARD_EXPENSES`, `INSURANCE`, `BANK_REVERSAL`, `UNIDENTIFIED`.

**Matching policy highlights**

1. Who paid ≠ who was invoiced: a parent pays for its subsidiaries; confirming banks and
   collection agencies pay on behalf of a customer.
2. Batches: one line = all receipts of the `direct_debit_batches/` file, all orders of a
   `card_settlements/` file, all cheques of a deposit, all documents of a `payment_runs/` file.
3. Remittance advices name the documents, the deductions and the reasons — the only
   source for 1:N and `SHORT_DISPUTE`. They may arrive after the cash.
4. Same amount, several open invoices → **oldest first (FIFO)**; a month in the text
   means that service month.
5. "Anula y sustituye": a payment citing a cancelled invoice settles its replacement.
6. Returns are often re-presented later (`re_presentation=Y`) or paid by transfer.
7. Deliberate drift: Moncayo migrates N43 → camt.053 on 2025-10-01, a key account changes
   its advice layout in 2025-09, and a ZAR subsidiary appears in FINAL.

---

## 8. Where to implement what

### `matching.py` — what does this bank line settle?

The baseline does three things: exact document reference in the text, party (IBAN or
fuzzy name) + exact amount, and a unique amount fallback. The TODOs mark the next
branches, roughly in order of value:

| branch | segment(s) | idea |
|---|---|---|
| 1:N references | `ar_1toN_text`, `ar_n_to_m` | sum the referenced items; accept when the total matches |
| remittance advice | `ar_1toN_remittance`, `ar_short_dispute` | parse the advice (csv/txt/pdf), use its rows and reasons |
| aggregate documents | `ar_dd_batch`, `ar_card_settlement`, `ap_confirming` | expand the batch/run file, then match each entry |
| credit notes | `*_credit_note_netted` | a payment can net an ARC/APC: add it as a positive row |
| FIFO / service month | `ar_same_amount_fifo`, `ar_same_amount_month` | break amount ties by issue date / text month |
| duplicates & returns | `*_duplicate*`, `*_returned*` | keep settled items indexed too; `RETURNED` re-opens |

### `rules.py` — what code goes on the row?

`exception_for()` is called once per emitted item row; `category_for()` once per line
that settles nothing. Both start from the folded bank text. Add the discount /
withholding / bank-charge arithmetic from the cheat sheet — each one is worth real points
in TRAIN, and the macro-F1 weight makes the rare classes matter.

### Workflow that actually works

```bash
# 1. look at the biggest gap
python - <<'PY'
import polars as pl
g = pl.read_csv("data/raw/sample/golden_allocations.csv.gz", infer_schema_length=0)
print(g.group_by("segment").agg(pl.len().alias("rows"), pl.col("bank_txn_id").n_unique().alias("lines")).sort("rows", descending=True))
PY

# 2. read 20 examples of that segment (join to bank_transactions, look at `text`)
# 3. implement ONE branch
PYTHONPATH=src python -m bankrec run --phase sample && \
PYTHONPATH=src python -m bankrec score submissions/submission_sample.csv.gz --phase sample
# 4. keep it only if the score went up; then try `--phase train`
```

Use `notebooks/` for exploration. Keep `src/` clean: the pipeline should be runnable
at any commit.

---

## 9. Performance notes

- **Polars** does all bulk IO/filtering (`data.py`); the matcher only touches candidate
  rows. Sample phase: **~5 s**. TRAIN (~1.5 M lines, 5.5 M items) is minutes and
  several GB of dict rows — if you go there, keep the per-item dict and add a date
  filter (an item issued after the cash cannot be settled by it).
- Never write `for item in all_items` inside the per-line loop: index by reference,
  amount and party (that is exactly what `ItemIndex` does).
- Cache anything derived from `masters/` once (600 k re-normalisations per line is how
  a 5-second run becomes a 30-minute one).

---

## 10. Checklist before you submit

- [ ] `make validate FILE=… PHASE=LIVE` → 0 problems, and every bank line is covered
- [ ] `make score` on `sample`/`train` improved (compare per-segment accuracy, not just total)
- [ ] no data leakage: you only used `golden` from TRAIN, never tried to obtain LIVE/FINAL labels
- [ ] `.csv.gz`, ≤ 60 MB uncompressed, ≤ 3 M rows
- [ ] FINAL: one shot — the first **valid** file counts, so validate on LIVE first

---

## 11. Reference material

- `docs/README_EN.md` — the challenge, phases, timeline
- `docs/POLICIES.md` — the rules the labels follow *exactly* (read this twice)
- `docs/FORMAT.md` — submission format + validation + scoring
- `docs/DATA_DICTIONARY.md` — every file and column
- `docs/official-starter/` — the organisers' naive baseline and the reference scorer
- `CLAUDE.md` — the decision log: why cents, why polars, why the matcher is
  conservative, why the book is stateful, what to leave alone
- `advanced/` — a fuller implementation (advice parsing, batch expansion, subset-sum,
  relaxed partials) kept as a reference, not as a starting point
