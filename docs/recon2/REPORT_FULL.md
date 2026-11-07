# recon2 — The Complete Engineering Report

**Bank reconciliation engine for the "Close the Books" hackathon (2026-11-07)**
What we built, how it works, why every decision was made, what we deliberately did *not* do,
how we measured it, and what can still go wrong.

> Reading guide
> * **Part I** (sections 1–3): the problem, the data, and the result in numbers.
> * **Part II** (sections 4–6): how we worked — the method is as important as the code.
> * **Part III** (sections 7–9): the engine, component by component, with the *why* of each rule.
> * **Part IV** (sections 10–12): rejected approaches, bugs we found, and how we kept ourselves honest.
> * **Part V** (sections 13–15): validation, known limits, the FINAL runbook.
> * **Appendices**: constants, file map, commands, glossary, the full score log.

---

# PART I — THE PROBLEM AND THE RESULT

## 1. The problem in one page

A synthetic multinational (17 legal entities, ~47 bank accounts, Spanish holding `ES00` with
subsidiaries in Europe, the Americas, Africa, Asia and Oceania) receives bank statements in three
formats (Spanish **N43**, SWIFT **MT940**, ISO 20022 **camt.053**). For **every single bank line** we
must answer one of two things:

* **Item rows** — which open items the cash settles: customer invoices (`ARI`), customer credit notes
  (`ARC`), supplier invoices (`API`), supplier credit notes (`APC`); for each, the **amount in the
  item's currency** and an **exception code** (or empty).
* **A category row** — if the line settles no item: what kind of movement it is (bank fees, payroll,
  tax, cash pool, intercompany, FX deal, …, or `UNIDENTIFIED`).

### 1.1 The score — and why it shapes every decision

```
score = 100 × ( 0.50 · allocation F1  +  0.30 · line accuracy  +  0.20 · exception macro-F1 )
```

| Term | What it counts | Consequence for design |
|---|---|---|
| **Allocation F1** (50 %) | each `(line, item)` row; a hit needs the right item **and** an amount within `max(0.01, 0.5 %)` of the truth | missing rows (recall) and wrong rows (precision) both cost; amounts must be exact enough |
| **Line accuracy** (30 %) | a line counts only if **every** row on it is right (items, amounts, exceptions; or the right category) | all-or-nothing: one wrong item on a 40-invoice line loses the whole line |
| **Exception macro-F1** (20 %) | per row, averaged over the **10 exception classes equally** | a class with 8 cases (bank charges) weighs as much as one with 466 (partial): rare classes are extremely valuable |

Three design principles follow directly and are used throughout this report:

1. **A wrong answer costs more than no answer.** A wrong row lowers precision *and* breaks the line;
   a missing row only lowers recall. Therefore the engine **only answers on evidence** — a document
   that lists the item, an exact reference, or a payer plus an amount that adds up exactly.
2. **Rare exception classes are worth engineering effort.** One correct `SHORT_BANK_CHARGES` row is
   worth roughly 70 times one correct line. Every exception rule was measured individually.
3. **Exceptions depend on time.** `PARTIAL` ("still open after this line"), `DUPLICATE_PAYMENT`
   ("already settled before") and `RETURNED` ("re-opens a settled item") are properties of the
   *state* of an item at a given moment, not of a row. Therefore the engine walks the bank lines
   **in date order** and keeps a running ledger of open balances.

### 1.2 The phases and the one-shot rule

| Phase | Period | Golden labels | Use |
|---|---|---|---|
| TRAIN | 2021-07 → 2025-06 | yes | learning and honest evaluation (time windows) |
| sample | June 2025 (inside TRAIN) | yes | fast 20-second check |
| LIVE | 2025-07 → 2025-12 | no | public leaderboard, unlimited submissions |
| FINAL | 2026-01 → 2026-06 | no | **one scored submission**, revealed at 20:30 |

FINAL brings announced drift: a new subsidiary `ZA01` in South African rand with a new document
numbering (`KWI…`), renamed customers (new master file `masters_final`), a new collections account
for `IT01`, a bank (Moncayo) that switched N43 → camt.053, and "slower payment in 2026".

## 2. The data landscape (what the engine reads)

* **Bank lines** (`bank_transactions.csv.gz`): signed amount in account currency, booking/value
  dates, bank codes, references (`ref1`, `ref2`), free text, and for camt: counterparty name/IBAN,
  end-to-end id, remittance info; for cross-currency: instructed amount and currency.
* **Items**: AR invoices, AP invoices, credit notes of the phase; for LIVE/FINAL also
  `open_items_at_start.csv.gz` — the balances carried over from the previous period.
* **Operation files** — documents that *contain the answer* for aggregate lines: direct-debit batches,
  card settlements, cheque deposits, our own payment runs (incl. confirming remittances).
* **Remittance advices** — customer letters listing which invoices a payment covers, in four formats
  (CSV, TXT e-mail, PDF, XLSX), with a layout change for two customers in September 2025.
* **PDFs** — invoices, credit notes, proformas, bank-account notices; some facts exist *only* there
  (instalment plans, retentions, advance deductions, "this invoice replaces X", rappel payouts,
  supplier credit-note offset vs refund, factoring assignments, new IBANs).
* **Masters** — customers (incl. parent company, trade name, IBAN, mandate), suppliers, entities,
  bank accounts, payment terms (with early-payment discounts), FX rates.

Measured facts about this data that drove the design (all from TRAIN golden):

| Fact | Number | Design consequence |
|---|---|---|
| Invoices − credit notes = cash on clean multi-item lines | **100 %** | arithmetic must be **exact**, not "close enough" |
| One-item lines where golden amount = cash | 99.91 % | same |
| Lines where the payer's amount is unique among its open items (noisy-reference segment) | **99.97 %** | "find the payer, then the amount" solves the largest segment |
| Unidentified receipts that equal some *unrelated* open invoice | **23 %** | **never match on amount alone** |
| Early-payment discount = round-half-up(% × total) | 100 % (±1 cent) | exact discount rule |
| Withholding rate per customer stable | 516 customers, 0 with two rates | learn the rate per customer |
| Bank charges ⇔ customer country ≠ entity country | 1,128 / 1,128 | exact gate (not a SEPA country list) |
| Advice payment date = bank booking date | 100 % (received date only 27 %) | link advices on payment date |
| Treasury lines with a counterparty IBAN | **0 of 19,250** | the "own-IBAN" approach can never work; use text |
| N43 text fragments of exactly 38 characters | 85,528 | names and numbers are cut by line wraps; re-glue |

## 3. The result in numbers

### 3.1 Score log (every LIVE submission, in order)

| Build | Main content | Sample | TRAIN May-25 | **LIVE** |
|---|---|---|---|---|
| old engine (`alt`) | the code we inherited | 78.65 | — | — |
| v1 | first end-to-end `recon2` | 87.66 | 82.08 | **81.12** |
| v2 | overpayment shape, unidentified discipline, party sweep, child gate, N43 runs, work budget | 90.16 | ~86.9 | **84.61** |
| v3 | lessons ported from an independent team's engine, crash fix | 92.04 | 87.04 | **85.57** |
| v4 | multi-invoice rules, FINAL readiness, late-discount rule | 92.23 | 87.88 | **86.13** |
| v5 | part-paid remainders, partial shapes, slash references, causality filter | 92.66 | 88.98 | **86.89** |
| v6 | mixed-currency supplier payment runs, payer bootstrap, confirming guard | 92.66 | **91.51** | **89.62** |

Held-out check (a TRAIN month we never tuned on): **February 2025 = 92.75** with v6.

### 3.2 Where the remaining points are (v6, TRAIN May)

* Allocation F1 0.881 — **precision 0.987, recall 0.796**: we are almost never wrong when we answer;
  we answer too little. The misses concentrate in a few very large multi-invoice lines.
* Line accuracy 0.931; exception macro-F1 0.850.

---

# PART II — HOW WE WORKED (THE METHOD)

The method matters as much as the code: in a few hours, with no labels for the phases that count,
the only way to improve reliably is to turn every idea into a **measured hypothesis** and to protect
the one irreversible action (the FINAL submission).

## 4. From evidence to design: the expert panel

Before writing a line of the new engine we ran a structured design debate with independent
reviewers, each with a different expertise and each required to ground every claim in the source
code or the data (never in the existing prose documentation, which was known to be outdated):

| Role | Expertise | What it contributed |
|---|---|---|
| **Architect** | algorithms, data structures, performance, determinism | the pipeline, indexes, arbitration, trace format, build plan |
| **Accountant** | AR/AP and bank reconciliation practice | the exception rulebook, measured on TRAIN; residues; refunds; instalments |
| **Treasury** | group structure: holding, cash pool, payment factory, factoring, confirming, agencies | treasury rules (0 false positives), payer precedence, entity scope |
| **Documents** | PDF/XLSX/CSV/TXT extraction, bank statement formats | extractor specs, the N43 wrap, the advice layout change, bank-code purity |
| **Red-team** | adversarial review: leakage, determinism, failure modes, time | killed two-pass "claims", the wall-clock deadline, forced the fast dev loop |
| **Verifier** | fast data checks on a stratified TRAIN sample | settled disputes with numbers in seconds |

Three exploration passes came first (source code of the old engine, the organisers' specification
and official scorer, and a statistical mining of TRAIN golden). The panel then iterated:
**proposal → critiques from every role → verification of disputed claims → revised proposal**,
until every reviewer accepted (two full rounds). Examples of disputes settled by data, not opinion:

| Dispute | Verified answer |
|---|---|
| "PARTIAL ⇔ item still open after the line" | false as stated (precision 0.55) because returns re-open items; true (0.97/0.995) once RETURNED is decided first |
| bank-charge limit 2 % (policy text) vs 3.15 % (golden) | golden wins (47 cases above 2 %); and the decisive gate is customer country ≠ entity country (100 %) |
| does the last instalment close the item? | decided **by amount** (last = total − (n−1)·⌊total/n⌋): 6,329/6,329; 726 rows close the item and are still PARTIAL |
| withholding base: net or total? | total (net never matches) |
| link advices on received date or payment date? | payment date (100 % vs 27 %) |
| SHORT_DISPUTE without an advice? | essentially never on one-item lines (2 vs 36,006 PARTIAL); on large lines it follows exact remainder percentages |

## 5. Rewrite, not patch — and why

The inherited engine (`alt`, sample 74–78) had grown by accretion. The code review found structural
bugs that patches could not fix cleanly:

* carried-over items stored their kind as the literal `"ARI"` instead of a credit-note flag, so
  **27,529 LIVE credit notes were never treated as credit notes**;
* **325,419 carried-over items had no payment terms**, so the early-payment discount could never fire;
* date windows were computed by subtracting `YYYYMMDD` integers (20250601 − 20250531 = 70 "days");
* the payer search stopped at the first source that returned anything, so a wrong counterparty name
  hid the real payer;
* inference accepted "the least bad" hypothesis even when the arithmetic did not add up;
* the bank-code map file it expected did not exist, so bank codes were never used.

We chose a near-complete rewrite (`src/recon2`, ~5,000 lines) with a frozen data contract, while
keeping the old engine as the shipping fallback until the new one beat it (it did at the first
end-to-end run: 87.66 vs 78.65 on the sample; 81.12 on LIVE at the first push). The old engine was
removed only after that, and `bankrec process` now runs `recon2` behind the same build registry,
validation and submission gate.

## 6. The measurement system (how we know a change helps)

| Instrument | Speed | What it tells us | Limitation |
|---|---|---|---|
| **sample** (June 2025, 8.5k lines, golden) | 20 s | fast regression gate | no ES00, no treasury/BR-MX-MA, no FX runs: blind to whole families |
| **TRAIN windows** `train:YYYY-MM` (33k lines, golden, LIVE-shaped open book) | ~2.5 min | honest per-segment gaps incl. ES00, treasury, FX | must not be over-used for tuning |
| **held-out window** (Feb 2025, never tuned on) | ~2.5 min | overfitting check | single month |
| **oracle harness** | 3 s | exception rules alone, given the true items and balances | does not test matching |
| **gap report** (`python -m recon2 gap`) | seconds | every miss bucketed by first failure and ranked by points | — |
| **LIVE pushes** (unlimited) | ~10 min | the real metric on unseen data | aggregate only; we never probe single lines |

How a TRAIN window is made LIVE-shaped: lines of the month, items issued up to the month end and not
closed more than 60 days before it, balances rebuilt by replaying golden **strictly before** the
window start (the exact equivalent of the organisers' `open_items_at_start` — verified: for 99.70 %
of LIVE's carried items, `open = total − Σ TRAIN golden`). The replay was originally filtered by
transaction id; a reviewer found that ids are not date-ordered across accounts (521 June lines would
have leaked), so it is filtered by date.

Learned artefacts are trained only on data **up to 2024-12-31** for every golden-scored evaluation
(router, withholding rates, payer aliases). A first alias table trained on all TRAIN leaked into the
sample; re-scored with the holdout table the sample moved 87.70 → 87.66, so the leak was negligible —
but every reported number since uses the holdout table.

### 6.1 The work loop

```
gap report → biggest bucket → root cause on 3–5 concrete lines → general rule (never a line-specific hack)
→ sample gate (must not drop) → TRAIN window → commit → LIVE build from a clean snapshot → push → log score
```

Rules for the loop, each learned the hard way:

* **One file, one owner.** Parallel workers edit disjoint files; one integrator merges. (Two workers
  in one file would silently overwrite each other.)
* **One heavy process at a time.** A LIVE build uses ~3 GB; several at once exhausted 31 GB of RAM and
  killed the session once.
* **Build LIVE from a clean snapshot of the commit**, never from a working tree another agent is editing.
* **Commit only what passed the gate**; partial work stays uncommitted.

## 6.2 Explainability built into the engine

Every bank line produces a trace record (`*.trace.jsonl.gz`): the route taken (treasury / ops /
advice / reference / payer+amount / router), the resolver and tier, the candidate payers **with the
source that found each one**, rejected candidates with a reason code, and the emitted rows with the
exception **rule id** (E0–E10). The gap report joins trace and golden and assigns every miss to its
first failure:

`NOT_GENERATED(reason)` (no hypothesis contained the item) · `REJECTED(reason)` (generated but failed
arithmetic or was ambiguous) · `CHOSEN_OTHER(resolver)` · `AMOUNT_OFF(rule)` · `EXC_WRONG(rule)` ·
`EXTRA_ITEMS(resolver)` · `WRONG_CAT` · `ITEM_EXPECTED`, each ranked by the score points it costs.

This is what made every later fix targeted: e.g. the largest single loss on TRAIN May was immediately
visible as "`ap_fx`: 26 lines, 2.12 points, NOT_GENERATED(ROUTER)".

---

# PART III — THE ENGINE

## 7. Architecture

```
            ┌──────────── load (once) ─────────────────────────────────────────────────────┐
 bank lines │ integer cents · day ordinals · lines sorted by (booking date, txn id)         │
 items      │ phase tables + carried-over balances rejoined to earlier tables · masters · FX│
 documents  │ ops files (all folders + lookback) · advices (4 formats) · PDF facts          │
            └───────────────────────────────────────────────────────────────────────────────┘
                              │
            pre-pass: same-phase payer bootstrap (learn payer names from certain lines)
                              │
  for each line, in date order ────────────────────────────────────────────────────────────┐
   1. treasury regex ─────────────────────────────────────► category (final)                │
   2. returns / ops files / advices / re-payment of a returned item   (tiers 0–1)           │
   3. document references (tiers 2–3)  +  payer + amount (tiers 4–5)                        │
   4. arbitration: exact fit first, stronger evidence, fewer items, oldest, item id         │
      split_cash → decide() → rows with amount + exception (+ rule id)                      │
      ledger update (open balances, history, residue flags)                                 │
   5. nothing fits → learned category router (credit → UNIDENTIFIED)                        │
   trace record for every line ──────────────────────────────────────────────────────────────┘
                              │
     writer guards (never a rejected file) → csv.gz → official validator → LIVE / FINAL
```

### 7.1 Foundational choices

| Choice | Why | Alternative rejected |
|---|---|---|
| **Single chronological pass with a mutable ledger** | PARTIAL / DUPLICATE / RETURNED are states in time | global optimisation (ILP / assignment over all lines) loses time order; two-pass "claims" (reserve items for strong lines) breaks duplicate detection, because a duplicate's *first* payment is usually the weakly-evidenced one |
| **Integer cents everywhere**, parsed from strings | tolerance is ±0.5 % / 1 cent; float drift flips boundary cases | floats |
| **Day ordinals** | date windows across month ends | `YYYYMMDD` arithmetic (old bug) |
| **Exact arithmetic** for same-currency fits (±1 cent only for rounded percentages) | 100 % of clean lines add up exactly; tolerance creates coincidences | the old 1 % tolerance |
| **"Never the least bad"**: a hypothesis whose sum does not explain the cash is rejected | wrong rows cost twice | accepting the smallest gap |
| **Never amount alone** | 23 % of unidentified receipts equal an unrelated invoice | global amount lookup |
| **Deterministic work budget** (counted steps, never the clock) | same input → same output (verified with two hash seeds) | wall-clock deadlines (output would depend on machine load) |
| **Frozen data contract** (`model.py`) | six builders worked in parallel on disjoint modules | shared mutable dicts |

### 7.2 Module map

| Module | Lines | Responsibility |
|---|---|---|
| `model.py` | 112 | the contract: Item, Line, Alloc, Hyp, Decision, `day()` |
| `money.py` | 72 | locale-proof amount parsing, no floats |
| `load.py` | 271 | phases, carried-over rejoin, masters per phase, TRAIN windows |
| `textviews.py` | 102 | text folding, N43 wrap repair, reference tokens |
| `book.py` | 182 | open items, indexes, ledger state |
| `fx.py` | 56 | ECB rates (exact fractions), missing-currency safety |
| `treasury.py` | 49 | treasury category regexes |
| `cats.py` | 117 | learned category router + overrides |
| `party.py` | 365 | payer resolution (sets with sources), aliases, bootstrap |
| `payer_ngram.py` | 124 | char 3-gram payer matcher (built, measured, not wired) |
| `ops.py` | 478 | batches, cards, cheques, payment runs (global index), mixed-currency runs |
| `returns.py` | 183 | returns, chargebacks, rejected transfers, re-presentation |
| `advices.py` | 431 | remittance advice parsers (CSV/TXT/PDF/XLSX) and linkage |
| `pdfclauses.py` | 235 | PDF clause facts |
| `resolve.py` | 811 | references, payer + amount family, sweeps, subsets, arbitration |
| `decide.py` | 382 | the exception table (pure function) |
| `engine.py` | 581 | orchestration, strict mode, bootstrap, error accounting |
| `writer.py` | 103 | guards; output file |
| `trace.py`, `gap.py`, `__main__.py` | ~330 | explainability and CLI |

## 8. Component by component — what, how, why

### 8.1 Loading and the carried-over book (`load.py`, `book.py`)

* **Kind = item-id prefix** (`ARI/ARC/API/APC`). *Why:* the old engine's literal-string bug lost
  27.5k credit notes.
* **Carried items rejoined** to earlier phases' tables (TRAIN for LIVE; TRAIN + LIVE for FINAL) by item
  id, restoring payment terms, payment method, PO and the credit note's related document. *Why:*
  without terms, discounts can never be recognised; all 325,419 LIVE carried ids exist in TRAIN.
* **Residue flag**: an item with 0 < open < total (the 2 % left by a late discount, a 5 % retention, a
  dispute remainder). *Why:* these are rarely paid (late-discount residue 2.4 %, advance residue 0 %),
  so they are excluded from amount-only and FIFO matching and reachable only by explicit evidence
  (reference, advice, release wording, n-to-m remainder, an exact part-paid remainder of a known
  payer). LIVE carries ~32k of them.
* **Entity always from the bank line**, never by joining the accounts master (one active `IT01`
  account is missing from it; `ZA01` already appears in LIVE).
* **Masters per phase** (`masters_final` for FINAL).
* **Indexes built once** (lesson from the old code: anything O(masters) per line turns seconds into
  hours): document-number variants (alphanumeric, digit core, without leading zeros, PO), sorted key
  lists for prefix and suffix search (one binary search each), supplier-qualified keys (bare supplier
  numbers collide across suppliers), open items per party ordered by due date, an index on the
  **current open balance** (not the original total), payment history per item.
* LIVE: 203,083 lines and 1.03 M items load in ~16 s; the book builds in ~11 s.

### 8.2 Reading the bank text (`textviews.py`)

* **N43 wrap repair.** N43 joins 38-character fragments with " | ". A fragment of exactly 38 characters
  followed by " | " is a wrap, not a separator, and is glued back without a space
  ("REDONDO JA | RDINES" → "REDONDO JARDINES"). 273k of 317k TRAIN N43 lines have several fragments.
* **Reference tokens**: alphanumeric reference-like strings (≥5 chars with a digit), hyphen/dot
  pieces, glued words split ("RECHNUNGRE250110905" → `RE250110905`), continuation lists expanded
  ("RE250106232 106234 106402" → also `RE250106234`, `RE250106402`), and **slash-joined numbers**
  ("F25/04150" → `F2504150`).
* *Measured:* of every golden (line, item) pair whose invoice number is visibly in the text, extraction
  + lookup finds **99.7 %** (7,932 of 7,955). Before the slash fix it was 90.3 %: 749 of the 772 misses
  were slash-numbered documents, one systematic bug found by measuring extraction recall directly. The
  remaining 23 are deliberate bank typos, handled later by payer-scoped fuzzy matching.

### 8.3 Treasury categories (`treasury.py`) — first, final answer

| Category | Pattern (literal) |
|---|---|
| CASH_POOL | `CASH CONCENTRATION`, `ZBA SWEEP TO HEADER`, `TRASPASO CASH POOLING`, `TRASPASO ENTRE CUENTAS ES00`, `TRASPASO PROPIO` |
| INTERNAL_TRANSFER | `TRASPASO A/DE CTA nnnn` (last 4 digits of our other account) |
| FX_DEAL | `FX DEAL nnnnnnnnnn`, `COMPRA/VENTA DIVISA <CCY>` |
| INTERCOMPANY | `FINANCIACION IC`, `PRESTAMO IC/INTRAGRUPO`, `TRANSF. IC VALTERM…`, truncated forms — checked **before** loan rules |

*Why literal text:* 19,244 / 19,250 TRAIN lines correct with **0 false positives** over 1.49 M lines.
The previous approach (the counterparty is one of our own IBANs) can never fire: that field is empty on
all 19,250 such lines. *Rejected:* bare keywords — 81 % of lines containing "POOL" on pool accounts
are customers who build swimming pools; "PRESTAMO" alone is also an ordinary loan repayment.

### 8.4 Returns and re-payments (`returns.py`) — tier 1

* A return is a line with the **opposite sign** to the item's normal direction plus return wording
  (R-codes, DEVOLUCION, RUECKLASTSCHRIFT, IMPAYE, CHARGEBACK, AC04 …), excluding bank reversals
  ("ANULACION OPERACION INDEBIDA") and retention releases.
* Linked to the original operation: direct debit by **mandate → latest collection on or before the
  return date** (the old engine took the *first* batch that ever used the mandate), or by the mandate
  in the customer master when no batch row explains it; cheque by cheque number in the deposit files
  (535 / 538); card chargeback by last-4 digits + merchant id + gross, searching ±1 month of settlement
  files; rejected supplier transfer by end-to-end id, including netted credit notes.
* Every item of the returned operation gets `RETURNED` and is **re-opened** in the ledger.
* **Re-payment rule**: a later payment from that party within 35 days goes to the re-opened item first.
  *Why:* FIFO would pick it only 22 % of the time; this rule matches every TRAIN case.

### 8.5 Operation files (`ops.py`) — tier 0, the strongest evidence

* Direct-debit batches, card settlements, cheque deposits and payment runs **list the exact items**;
  the batch / settlement / deposit id appears in the text 100 % of the time.
* **Global index.** Payment runs are indexed across **all entity folders** by debtor account, run id
  and end-to-end id, plus 4 months of the previous phase (late confirming debits) and 2 months for
  other files. *Why:* the holding `ES00` pays its subsidiaries' suppliers but those runs are filed in
  the subsidiary's folder (52k + 39k + 13k rows); per-folder loading would lose ~33k lines.
* **Run rows → items by (entity scope, document number, amount)**, never by beneficiary for the four
  factoring companies (they are paid *instead of* the supplier; 37,812 run rows). AP entity scope: an
  `ES00` account may settle `ES00/ES01/ES02/PT01` items (payment factory); everything else stays in its
  own entity.
* **Amount gate**: an operation answers only if the cash equals its total, one end-to-end group, or an
  exact sub-sum (bounded by a stated count such as "125 RECIBOS"), with the right sign. *Why:* the fee
  line "COMISION REMESA 03250603661 125 RECIBOS" (−18.75) reuses the batch id of an 89,776.76 batch;
  without the gate it expanded into 125 phantom rows.
* **N43 truncated end-to-end id.** Spanish N43 lines carry `PR…-0` in ref2 and the transfer sequence as
  the last 6-digit token of the text; the run key is rebuilt as `<run>-<seq:04d>`.
* **Mixed-currency payment runs (`ap_fx`).** A single run mixes USD and EUR groups; the bank debits the
  USD groups plus the EUR groups converted at the bank's rate (~0.8 % off ECB) and states the group
  count ("425 TRANSFERS"). Each group is converted at the execution date; the run is accepted when the
  stated count equals the group count and the converted sum is within 3 % of the cash (1 % without a
  count). This one rule moved TRAIN May **89.11 → 91.51**: 26 lines, but each with hundreds of rows.
* **Cards** clear orders at **gross** while the bank credits the net; the acquirer fee is contractual,
  not an exception.

### 8.6 Remittance advices (`advices.py`) — tier 0

* Four formats parsed by **header synonyms** (EN/ES/DE/FR): CSV with `,` or `;`, TXT e-mails (English
  header dates are US `mm/dd`, Spanish `dd/mm`), PDF tables, XLSX — including the September 2025 layout
  ("Voucher | Invoice | PO | … | Net paid") where the old parser silently read the voucher as the
  document and the invoice digits as the amount.
* Linked to a bank line by **payment date = booking date and Σ paid = |cash|** (same currency; the
  payment reference in the text breaks ties).
* Each advice row carries a reason: a dispute reason → `SHORT_DISPUTE`; "RET" in `BR01` → withholding at
  gross (18,364 / 18,364).
* *Measured on the sample:* **327 of 327** linked lines produce the exact golden item set.

### 8.7 PDF facts (`pdfclauses.py`)

`pypdf` text (synthetic, text-based PDFs; no OCR), wraps re-joined, a closed English/Spanish regex table
(clauses never appear in other languages), every amount through the same `money()` parser. Facts:
instalment plan (n, amounts), retention (%, amount), advance deducted, "replaces invoice X", service
month, rappel offset/payout, supplier credit-note offset/refund, cancellation, factoring assignment,
IBAN changes. **Gate on LIVE: every family count matches exactly**
(1308 / 916 / 276 / 1694 / 565 / 940+409 / 884+510 / 352 / 1497 / 79+79), and the internal invariants
hold 100 % (instalments sum to the total; retention + payable = total). *Why rewritten:* the old parser
found 155 of 916 retentions, never detected a rappel payout, and read "2,328.37" as 232 cents
(comma-only decimals), so every English instalment plan was empty.

### 8.8 References (`resolve.py`, tiers 2–3)

Search order: exact variant → PDF "replaces X" → suffix (our prefix dropped) → prefix (truncated at
16/35/38 chars) → glued / continuation tokens → one typo or adjacent swap **inside the payer's items
only**. Amounts decide among a payer's items sharing a truncated reference. Named items must pass the
arithmetic; if they don't, the engine tries netting up to 3 open credit notes of the party, and
**named + remainder**: the named items plus a unique subset (≤4) of the same payer's other open items
that completes the cash exactly.

### 8.9 Who paid (`party.py`)

The payer is a **set** of candidate parties, each tagged with its source — never a single guess (7,029
names are shared by 23,393 customers in one entity). Precedence:

1. **Third-party markers replace the counterparty**: collection agency `EXPTE <n> <debtor> <doc>`,
   confirming bank `ORD. <debtor> FRA <doc> VTO ddmmyy` (doc re-glued across the N43 wrap),
   `POR CTA DE <child>`. An agency or factor without a named debtor → no party (documents then resolve
   party-free).
2. IBAN (masters + old and new IBANs from bank-notice PDFs; SEPA creditor ids `…ZZZ…` excluded).
3. N43 8-digit ordering id learned from TRAIN (3,104 of 3,111 ids map to one customer).
4. Learned aliases (payer string → small party set, ≥5 % share).
5. camt counterparty name; payer markers (`TRANSFERENCIA DE`, `/ORDP/`, `ABWA+`, `VIREMENT DE` …).
6. Name index: exact/prefix name or trade name (handles truncation; ≤30), else rarest-token search (≤6).
7. **Same-phase bootstrap** (pre-pass): payer strings learned from lines of the *current* phase whose
   payer is certain (IBAN, or an exact reference to one party's item), ≥2 consistent hits, purity ≥0.9
   — used only when nothing else found a payer. *Why:* covers renamed customers and the new `ZA01` in
   FINAL, which no TRAIN alias can know.

**Parents and children.** A parent payer expands to its children (`parent_customer_id`), one hop,
directed. Children may only be used with strong evidence (exact reference, unique exact subset, or a
marker naming the child) — never by FIFO, single-amount, short-payment or month picks (on TRAIN,
child picks through those matchers were wrong 9 of 12 times).

### 8.10 Payer + amount (tiers 4–5)

For a resolved payer set: unique exact open amount (`party.amount`); several equal → the month named in
the text picks the invoice issued the following month (100 % when named), else oldest due
(`party.fifo`); exact subsets (`party.subset`, with ≤3 credit notes `party.subset_cn`); oldest-due
prefix sweeps with ≤3 credit notes or ≤4 gaps, accepted only if unique (`party.sweep_cn`,
`party.sweep_gap`); unique 2–4 invoice subsets over a 730-day pool (`party.uniq`); shapes on a single
untouched item — discount, withholding (per-customer rate subsets), bank charges, overpayment surplus,
instalment pieces, partial percentages (`party.short`, `party.over`, `party.wht`); part-paid remainders
(`party.resid`). Exclusions: residues, items outside the window, and a **causality filter** (an item is
never paid before it is issued — 0 violations in 5.16 M TRAIN rows).

### 8.11 Arbitration

Lexicographic: arithmetic fit level (exact > explained by an exception shape > tolerance) → evidence
tier → fewer items → oldest due → item ids. Exactness beats order: a truncated reference resolving to
one 1,265.84 invoice must not beat the 42-invoice sweep that sums exactly to the 100,423.72 on the
statement.

### 8.12 The exception table (`decide.py`) — a pure function

Per row, first match wins; discount and withholding are decided **per line**:

| Rule | Condition | Exception | Amount |
|---|---|---|---|
| E0 | card order | — | gross |
| E1 | return line | RETURNED | re-opened amount |
| E2 | item already settled, same amount again, AR ≤15 d / AP ≤50 d, one-item line (AP: also a repeated partial piece) | DUPLICATE_PAYMENT | cash |
| E3 | netted credit note | — | full |
| E4 | advice row with any stated non-discount, non-RET deduction | SHORT_DISPUTE | paid |
| E6 | instalment plan known; piece ≠ last | PARTIAL (even if it closes the item) | paid |
| E5 | cash covers the open balance | OVERPAYMENT only for shaped surpluses on a one-item first payment of an AR invoice; FX_DIFFERENCE if cross-currency; else — | open |
| E7 | discount terms, booked ≤ issue + discount days + 3, line identity holds | SHORT_DISCOUNT | total |
| E8 | per-customer withholding rate, line identity holds | SHORT_WITHHOLDING | total |
| E9 | one-item AR first payment, customer country ≠ entity country, short ≤ 71.10 and ≤ 3.2 % | SHORT_BANK_CHARGES | total |
| E10 | anything else short | PARTIAL | paid |

Why each non-obvious choice:

* **E6 before E5** — the last instalment is identified by amount; 726 TRAIN rows close the item and are
  still PARTIAL (pieces paid out of order).
* **E7/E8 per line** — 61 % of discount rows and 74 % of withholding rows sit on multi-invoice lines,
  where one row's shortfall is not observable; the identity `cash = Σ(total − deduction) − Σ credit
  notes` holds on 4,829 / 4,829 clean lines. Late discounts are PARTIAL (3,232 cases, zero overlap with
  the window). When a late discount covers several rows, the unique 1–3 row subset that explains the
  shortfall gets PARTIAL.
* **E8 per customer** — `BR01`/`MA01`: 1.5 % or 10 %; `MX01`: 1.25 % or 10 %; `ES01` only for Moroccan
  customers (otherwise a 10 % short is a retention → PARTIAL).
* **E9 by country, not SEPA membership** — Polish and Maltese payers are flagged in golden; 1,128 /
  1,128.
* **E5 OVERPAYMENT only for shaped surpluses** (1, 2, 5, 10, 50, 100 units or 1 % / 10 %) — 18 of 29
  earlier OVERPAYMENT rows were tolerance coincidences; every true one has a shaped surplus.
* **E4 for any stated deduction** — the organisers' examples treat any explained deduction that is not a
  discount or withholding as a dispute.
* Only AR invoices receive OVERPAYMENT and SHORT_*; FX_DIFFERENCE replaces empty / SHORT_* on fully
  cleared cross-currency invoices; PARTIAL and SHORT_DISPUTE keep their code; credit notes stay empty.
* *Measured (oracle, true items given):* every class F1 ≥ 0.95; macro 0.995 with advice facts.

### 8.13 Category router (`cats.py`) — only when no item evidence exists

Learned on TRAIN ≤ 2024-12: key (format, bank code, subcode, sign, first two words with digits masked)
with back-off levels so an unseen bank still routes. **0.9999** on 2025 category-only lines. Overrides:
proforma credit → CUSTOMER_ADVANCE; a debit equal to an earlier customer duplicate on the same account
within 47 days → CUSTOMER_REFUND (2,562 / 2,611); a credit equal to an earlier supplier duplicate →
SUPPLIER_REFUND; interest by sign; never CUSTOMER_REFUND or a treasury category on text carrying
invoice or payment-run markers (that guard corrected ~2,200 LIVE lines that were outgoing payments).
A credit with no evidence → `UNIDENTIFIED` + exception `UNIDENTIFIED`, **except** when a strong payer
had candidates (then the category without the exception) — re-measured on three windows, it is the
better rule (sample 92.66 vs 92.37 without it; February and May within ±0.02).
*Why the router never vetoes items:* bank codes cannot tell "unidentified" from "customer payment"
(95.6 % vs 4.1 % under the same code).

### 8.14 Safety (`writer.py`, `engine.py`, `fx.py`)

* **Writer guards** — malformed item id, amount ≤ 0, duplicate (line, item), mixed item/category rows,
  OVERPAYMENT/SBC/DUP on a multi-item line → the line is demoted to its category answer. *Why:* the
  server rejects the **whole file** on a single bad row.
* **Strict mode** for LIVE/FINAL — abort if any module degraded to a stub or per-line errors exceed
  0.1 %; explicit escape `RECON2_STRICT=0` for the FINAL release.
* **A missing FX rate refuses to convert** (no cross-currency match) instead of inventing
  1 ZAR = 1 EUR (≈19.5 in reality), which would create false matches.
* **Coverage check** — the written file must contain every bank line exactly once.
* **Work budget** — candidates ≤250, subset ≤40 values / 12 picks / 200k states, ≤24 subset searches per
  line, run sub-sums ≤80 groups / 30k states. *Why the per-line cap:* after the party sweep was added,
  key-account lines took up to 9 s each and a LIVE build exhausted RAM; the cap restored speed with no
  score loss (sample 90.16 → 90.16).

## 9. What the official examples confirmed

The organisers' worked examples (`/examples.json`, one per segment) match our conventions: amounts in
the **item** currency (an EUR receipt of a USD invoice writes the USD amount with `FX_DIFFERENCE`),
credit notes as positive amounts, PARTIAL on instalments and partials. One example confirms a rule we
found but did not ship: an `ar_short_dispute` line carries **no advice and no explanation**, yet golden
flags the disputed invoices purely by amount — paid at exactly 80 % and 70 % of their totals
(3,153.56 / 3,941.95 and 10,937.15 / 15,624.49), the "3/5/10/20/30 % remaining" pattern seen in 213 of
213 sample deductions. See section 14.

---

# PART IV — WHAT WE DID NOT DO, WHAT BROKE, AND HOW WE STAYED HONEST

## 10. Approaches we considered and rejected (with reasons)

| Approach | Why it was attractive | Why we rejected it (evidence) |
|---|---|---|
| **Patch the old engine** | lower risk under time pressure (the red-team's recommendation) | structural bugs (credit-note kind, missing terms, date arithmetic, first-source payer, least-bad acceptance) and an 18-point gap to the leaderboard top; a rewrite with the old engine as fallback beat it at the first run (sample 87.66 vs 78.65) |
| **Global optimisation** (ILP / assignment over all lines) | globally consistent | destroys time order → loses PARTIAL, DUPLICATE_PAYMENT and RETURNED, which are states in time |
| **Two-pass "claims"** (strong lines reserve items first) | protects items needed later | a duplicate's *first* payment is usually weakly evidenced (ar_ref_noise 2,820, ar_1to1 1,580); claims would hand the item to the duplicate and destroy duplicate detection; same for instalment pieces |
| **Tolerance matching (1 %)** | more matches | 100 % of clean lines add up to the cent; tolerance only adds coincidences (18 of 29 OVERPAYMENT rows were tolerance artefacts) |
| **Amount-only matching** | high recall | 23 % of unidentified receipts equal an unrelated open invoice |
| **"Accept the least bad hypothesis"** | never leave a line empty | wrong rows cost twice (precision and line accuracy) |
| **Own-IBAN rule for treasury lines** | textbook approach | counterparty IBAN is empty on 19,250 / 19,250 treasury lines |
| **Bare keywords** (POOL, PRESTAMO, SWEEP) | simple | 81 % of "POOL" lines on pool accounts are customers in the pool business; PRESTAMO is also loan repayment |
| **SEPA country list for bank charges** | matches the policy wording | golden flags Polish and Maltese payers; the real gate is customer country ≠ entity country (1,128 / 1,128) |
| **Union-find (DSU) over parties** | handles parent/child payers elegantly | measured upside ≤ +0.05: wrong-party lines have no relation to the right party; a union would make siblings interchangeable (sibling payments ≈ 0); replaced by a gated directed parent → child map |
| **group_id / tax_id as party links** | "same group" | sibling payments ≈ 0; 285 tax ids are shared by 575 *unrelated* customers (generator collisions) |
| **Wall-clock deadlines per line** | bounded runtime | output would depend on machine load; replaced by counted work budgets |
| **ML ranking of candidates** | learn the generator's choices | the bottleneck is candidate generation (recall), not ranking; a model on top of missing candidates learns the wrong thing |
| **Small LLM decision model (Kev-0.8B)** | robust to new wording | ~0.65–0.70 zero-shot vs our router 0.9999; one forward pass per line is hours on CPU; does not solve item selection; no similarity output for payer matching |
| **JEPA-style models for extraction** | general document understanding | documents are template-generated; extraction is already 99.7 % (references) and 100 % (advice chain, PDF clause counts); a model adds noise and non-determinism |
| **Char n-gram payer matcher as the main payer source** | handles spelling variants | built and measured: 99.1 % precision at 27 % coverage, but rescues only 6.4 % of no-payer lines (they carry no name resembling a customer); kept as an optional weak vote |
| **Dispute-subset solver before the freeze** | official example confirms the pattern | judged too risky for the remaining time (20–30 min of code, ambiguous on large lines); left as the top next step |
| **"Always mark UNIDENTIFIED"** | simpler | re-scored on three windows: worse on the sample (92.37 vs 92.66), neutral elsewhere |
| **Removing the causality filter** (−0.02 on the sample) | tiny score gain | the rule is never violated in 5.16 M TRAIN rows; the dip is tie-break noise, and it protects FINAL |

## 11. Bugs found and fixed (chronological)

| # | Bug | How it was found | Effect of the fix |
|---|---|---|---|
| 1 | Old engine: carried credit notes not recognised; carried items without terms | code exploration | designed out of `recon2` (kind = id prefix; rejoin) |
| 2 | Old engine: `YYYYMMDD` date arithmetic | code exploration | day ordinals |
| 3 | PDF clause parser: comma-only decimals, missing English anchors | documents brief vs LIVE PDFs | new parser; exact LIVE counts |
| 4 | Advice parser misread the Sep-2025 XLSX layout (voucher as document) | documents brief | new header-driven parser |
| 5 | Sample balance replay filtered by txn id (521 June lines leaked) | code review | filter by date; hash unchanged → honest baseline 87.66 |
| 6 | Alias table trained on all TRAIN (leak into evaluations) | code review | holdout table for every scored number (87.70 → 87.66) |
| 7 | FINAL carry-over rejoin only against TRAIN tables | code review | rejoin against TRAIN + LIVE |
| 8 | N43 payment runs: truncated end-to-end id | both reviewers independently | run key rebuilt from ref2 + sequence |
| 9 | Party sweep exploded on key accounts (9 s/line, RAM exhaustion) | slow-line tracing on LIVE | per-line subset budget; no score loss |
| 10 | Empty-candidate crash (`min()` of an empty list) swallowed per line | error summary | guard; sample +0.15 |
| 11 | Children of a parent reached FIFO/amount matchers (9 / 12 wrong) | DSU board | child gate |
| 12 | OVERPAYMENT on tolerance coincidences | accountant review | shape rule; +1.14 |
| 13 | Slash-numbered documents split by the tokenizer | extraction-recall audit | recall 90.3 % → 99.7 % |
| 14 | Mixed-currency payment runs never matched (`ap_fx`) | TRAIN-window gap report | TRAIN May 89.11 → 91.51; LIVE 86.89 → **89.62** |
| 15 | Missing FX rate treated as 1:1 | review of the FINAL-readiness change | refuse to convert instead |
| 16 | Strict mode could leave FINAL without a file | ideas board | explicit escape hatch |
| 17 | Push script read the interim "score=None" | LIVE log | read the final score |

## 12. How we kept ourselves honest

* **No LIVE/FINAL labels were ever used.** LIVE was measured only through the official leaderboard,
  as whole-engine submissions; we never probed individual lines.
* **Every golden-scored number uses artefacts trained up to 2024-12** and balances rebuilt strictly
  before the evaluated period.
* **A held-out month** (February 2025) that no fix was ever tuned on scored 92.75 — higher than the
  month we did tune on (91.51) — so the rules generalise rather than memorise.
* **Two independent reviewers** reached the same top bug (N43) from different evidence; an external
  team's engine was reproduced (86.03, claimed 86.36) and audited for leakage before we learned from it.
* **Determinism**: identical decompressed output across hash seeds; deterministic work budgets.
* **The irreversible step is protected**: FINAL keeps a typed human confirmation; strict mode refuses
  degraded builds; the official validator runs before every upload.

---

# PART V — VALIDATION, LIMITS AND THE FINAL RUN

## 13. Validation summary

| Window | Role | v6 score | F1 | Line acc. | Exception macro-F1 |
|---|---|---|---|---|---|
| sample (Jun 2025) | fast gate | 92.66 | 0.917 | 0.957 | 0.905 |
| TRAIN May 2025 | tuning window | 91.51 | — | — | — |
| TRAIN Feb 2025 | **held-out** | **92.75** | 0.944 | 0.937 | 0.874 |
| LIVE (Jul–Dec 2025) | leaderboard | **89.62** | — | — | — |

Speed: LIVE (203k lines, 1.03 M items) in ~9.5 minutes, ~3 GB RAM. Every LIVE file validated with 100 %
of lines covered.

## 14. Known limitations and next steps (ranked by expected points)

1. **Dispute-subset solver** — remainders of exactly 3/5/10/20/30 % (and 40/50/60 % for n-to-m), unique
   solutions only; confirmed by the official example. ar_short_dispute + ar_n_to_m ≈ 0.75 points on
   February.
2. **Credit-note netting on multi-invoice receipts** — ar_1toN_credit_note ≈ 0.7 points.
3. **Large sweeps of a known payer** (ar_ref_noise) — many equal-sum subsets; needs a tie-break signal
   (per-payer payment-lag prior: within-payer lag spread 13 days vs 33 overall).
4. **Lines with no payer** — mostly no usable name; candidates: wiring the n-gram vote (+0.1), payer-free
   matching for unique exact amounts within an entity only with a corroborating marker.
5. **Confirming remittances paid in slices** — ambiguous by amount alone; needs slice-order evidence.
6. **FINAL drift** — renamed customers (bootstrap helps), ZA01 without learned aliases or withholding
   rates, possible new treasury wording; time windows learned on TRAIN may be short for "slower payment
   in 2026" (duplicate ≤15/50 days, re-payment ≤35 days, pools ≤730 days).
7. **Over-specific constants** to watch: shapes (overpayment surplus set, partial percentages
   30–70 / 98 %, bank charges ≤71.10 / 3.2 %), literal names (group name, factors, agencies).

## 15. FINAL runbook

```
1. download FINAL into data/final  (check: ls data/final data/final/masters_final)
2. grep -c ZAR data/final/masters_final/fx_rates.csv        # if 0: ZA01 cross-currency matches are skipped (safe)
3. PYTHONPATH=src uv run python -m recon2 run --phase final --out out/recon2/final.csv.gz 2>&1 | tee out/recon2/final.log
      - strict mode aborts on a degraded module or >0.1 % per-line errors
      - only if the abort is understood and harmless: RECON2_STRICT=0 … (same command)
4. check the log: "modules … integrated", FX warnings, per-line error summary, lines with items
5. gzip -dc out/recon2/final.csv.gz | sha256sum               # record the hash
6. PYTHONPATH=src uv run python -m bankrec validate --data <data root> out/recon2/final.csv.gz --phase final
7. the owner submits:  make push PHASE=FINAL BUILD=<file>   and types  SUBMIT FINAL
```

---

# APPENDICES

## A. Key constants (current code)

| Constant | Value | Where | Evidence |
|---|---|---|---|
| amount tolerance (scorer) | max(0.01, 0.5 %) | vendored scorer | contract |
| fit for same-currency lines | exact (±1 cent for rounded %) | `resolve.py` | 100 % of clean lines |
| cross-currency band | ≤3 % one-sided spread | `decide.py` `FX_BAND_BP=300` | 96.3 % within 3 % at the booking-date rate |
| candidates per query | 250 | `resolve.py` `FANOUT` | performance + precision |
| subset search | ≤40 values, ≤12 picks, ≤200k states; ≤24 searches per line | `resolve.py` | LIVE runtime |
| bitset subset threshold | 20,000,000 cents | `resolve.py` `BITSET_MAX` | speed |
| payment-run sub-sums | ≤80 groups, ≤30k states | `ops.py` | LIVE runtime |
| lookback | runs 4 months, other files 2 | `ops.py` | late confirming debits |
| duplicate window | AR 15 d, AP 50 d | `decide.py` `DUP_WINDOW` | AR max 15, AP 14–50 |
| re-payment after return | 35 d | `returns.py` `REPRESENT_DAYS` | 100 % within 35 d |
| discount grace | +3 days | `decide.py` | 38,854 vs 3,232, zero overlap |
| late discount on multi-invoice lines | +7 days | `decide.py` `LATE_MULTI` | measured on golden |
| withholding defaults | BR01/MA01 1.5 % or 10 %; MX01 1.25 % or 10 % | `decide.py` `WHT_DEFAULTS` | 8,672 / 8,672 |
| bank charges | ≤71.10 and ≤3.2 % | `decide.py` `SBC_MAX`, `SBC_MAX_BP` | golden max 71.10 / 3.15 % |
| payer name matches | prefix ≤30, token search ≤6 | `party.py` | shared names |
| strict error budget | 0.1 % of lines | `engine.py` `MAX_ERR_RATE` | safety |

## B. Commands

```
PYTHONPATH=src uv run python -m recon2 run --phase sample                   # 20 s, prints SCORE
PYTHONPATH=src uv run python -m recon2 run --phase train:2025-05            # LIVE-shaped TRAIN window
PYTHONPATH=src uv run python -m recon2 gap --phase train:2025-05 --sub <csv> # miss buckets by points
PYTHONPATH=src uv run python -m recon2.dev.oracle                           # exception table only
PYTHONPATH=src uv run python -m recon2 run --phase live                     # LIVE build (~10 min)
scripts/live_push.sh <file> "<label>"                                       # validate + push LIVE + log score
RECON2_ALIASES=artifacts/aliases_2024.json                                   # holdout aliases for scored evaluations
RECON2_STRICT=0                                                              # strict-mode escape hatch
```

## C. Glossary

* **Item** — an open invoice or credit note (ARI customer invoice, ARC customer credit note, API supplier
  invoice, APC supplier credit note).
* **Tier** — strength of evidence: 0 operation file/advice, 1 return/duplicate/re-presentation,
  2 exact reference, 3 noisy reference, 4 payer + unique amount, 5 payer + FIFO/subset.
* **Residue** — what is left open on an item after a partial payment (late discount, retention, dispute).
* **Sweep** — a payment covering a run of a payer's oldest-due invoices.
* **Payment factory** — the holding `ES00` paying suppliers of `ES01/ES02/PT01`.
* **Confirming / factoring** — a bank or finance company paying (or being paid) on behalf of a party.
* **N43 / MT940 / camt.053** — Spanish, SWIFT and ISO 20022 bank statement formats.
* **Golden** — the organisers' correct answers, available only for TRAIN and the sample.

## D. Where everything lives

* Engine: `src/recon2/` · design: `docs/recon2/DESIGN.md` · module APIs: `docs/recon2/API.md`
* Debate and review record: `docs/recon2/debate/` (index in its README)
* Learned artefacts: `artifacts/` (router, aliases, withholding rates)
* Tests: `tests/recon2/` · CLI: `python -m recon2` · submission gate: `src/bankrec/submit.py`

## E. LIVE score log

| Time | Build | LIVE |
|---|---|---|
| 16:20 | v1 first end-to-end engine | 81.12 |
| 16:56 | v2 | 84.61 |
| 17:12 | v3 | 85.57 |
| 17:23 | v4 | 86.13 |
| 17:40 | v5 | 86.89 |
| 18:03 | v6 (mixed-currency payment runs, payer bootstrap, confirming guard) | **89.62** |
