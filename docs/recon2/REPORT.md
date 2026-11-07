# recon2 — how it works, why, and what can go wrong

Two versions of the same story:
* **Part A — plain language.** No jargon. Read this first.
* **Part B — technical.** Same decisions, with the exact rules, numbers and failure modes.

Status at the time of writing (16:10): sample score **87.66** (old engine `alt`: 78.65).
Not yet proven: LIVE end-to-end run, LIVE validation, determinism on LIVE.

---

# PART A — plain language

## A1. What is the job?

A company gets a bank statement every day. Every line says "money came in" or "money went
out", with a short, messy text. Separately, the company has a list of **bills it sent to
customers** (they owe us) and **bills it received from suppliers** (we owe them).

For every bank line we must say **which bills that money paid**, how much of each, and
whether something unusual happened (the customer paid a bit less, paid twice, the payment
bounced…). If the line is not about a bill at all (bank fee, salary, tax, moving money
between our own accounts), we say what kind of movement it is.

A judge compares our answers with the true answers and gives a score out of 100.

## A2. How is the score built, and why does that shape everything?

Three parts:
1. **50% — did you name the right bills with the right amounts?** Counted bill by bill.
2. **30% — is the whole line perfectly right?** One mistake on a line = the whole line is wrong.
3. **20% — did you spot the unusual situations?** There are 10 kinds ("paid less because of
   a discount", "paid twice", "bounced"…). Each kind counts the same, even if it is rare.

What that implies:
* **A wrong guess is worse than no guess.** It costs you in parts 1 and 2. So we only answer
  when we have proof — like an accountant who refuses to book a payment "because the amount
  looks similar".
* **Rare situations are very valuable.** "Bank charges deducted" happens ~10 times in the sample
  but is worth as much as "partial payment" which happens ~500 times. So we spent real effort
  on the exact rules for each unusual case.
* **Order in time matters.** "Paid twice" only makes sense if you remember it was already paid.
  So we read the bank lines **in date order** and keep a running notebook of what is still owed.

## A3. The big idea: find proof, in order of how strong it is

For each bank line, we ask a series of questions, strongest proof first. The first question
that gives a proof that **adds up to the exact amount** wins.

1. **Is it an internal treasury movement?** (money between our own accounts, loans between
   our companies, currency deals). These have very fixed wording, so we recognise them by text.
2. **Is it a bounced payment?** If so, find the original payment and undo it.
3. **Is there an official list attached?** Direct-debit batches, card settlements, cheque
   deposits and our own payment runs come with files that list exactly which bills are
   included. That is the best proof possible.
4. **Did the customer send a payment advice?** (an e-mail/PDF/Excel saying "this payment
   covers invoices X, Y, Z, minus 60 € for damaged goods"). Also excellent proof.
5. **Does the bank text mention an invoice number?** Often mangled: cut short, missing the
   prefix, typos. We search smartly for those.
6. **No number at all?** Find **who** paid (from the bank account, the name, a known ID…) and
   look at what that customer owes. If exactly one open bill has exactly that amount, that's it.
7. **Nothing works?** Then it is probably not about a bill: we classify the movement
   (bank fee, tax…) or, if it's money in that we can't explain, we say "unidentified".

## A4. Why exact amounts?

Because in this data, when a customer pays several bills at once, the bills minus any credit
notes add up **to the cent** in 100% of normal cases. Allowing "close enough" (the old engine
allowed 1%) mostly creates coincidences: a 1,000 € payment "matches" some random 1,005 € bill.
The only exceptions are known situations (discount, withholding tax, bank charges, currency
exchange), and each has its own exact formula.

## A5. The unusual situations, in plain words

* **Discount**: the customer pays early and takes the agreed % off. Only valid if paid within
  the deadline (+3 days of grace). Paid late with the discount anyway → it's just a partial payment.
* **Withholding tax**: some customers in Brazil, Mexico, Morocco must keep a fixed % for the tax
  office. Each such customer always uses the same %.
* **Bank charges**: a foreign customer's bank took a small fee in the middle. Only when the
  customer's country differs from our company's country.
* **Partial payment**: they paid part, the rest is still owed.
* **Dispute**: they paid less and *told us why* (damaged goods, price difference…) in an advice.
* **Overpayment**: paid more than owed on a single bill.
* **Paid twice**: the bill was already fully paid and the same amount comes again.
* **Bounced**: a direct debit, cheque or card payment came back.
* **Currency difference**: paid in another currency; small exchange-rate differences are normal.
* **Unidentified**: money arrived and we genuinely can't tell what for.

## A6. What could go wrong (in plain words)

* **The new data (LIVE/FINAL) may look different** from the training data: a new subsidiary in
  South Africa, a bank that changed its statement format, customers who changed bank accounts.
  We built in fallbacks (rules don't depend on a specific bank's wording where possible), but
  a brand-new pattern will fall to "unidentified" or a category.
* **One early mistake can spread.** If we wrongly mark a bill as paid, a later true payment of
  that bill may look like "paid twice". We soften this for bills named explicitly.
* **We may be too cautious.** Refusing to guess protects the score, but some correct answers
  are left on the table (e.g. customers that pay without any reference *and* whose name we
  don't recognise).
* **It has only been fully measured on one month (the sample).** The modules were each checked
  on years of training data, but the full engine has only run end-to-end on the sample so far.
* **Time.** It was built in about two hours. The safety net: the old engine stays ready, and
  nothing is uploaded without a human typing the confirmation.

---

# PART B — technical

Score = 0.5·F1(rows, amount tol max(0.01, 0.5%)) + 0.3·line accuracy (all-or-nothing) +
0.2·exception macro-F1 (per row; empty is not a class; classes absent from golden are free;
TP ignores amount). Code: `src/recon2/`. Design: `DESIGN.md`. API: `API.md`.

Each decision below: **Choice → Why (evidence) → Alternatives rejected → What can go wrong.**

## B1. Engine shape: one chronological pass with a mutable book

* **Choice**: sort lines by (booking date, bank_txn_id); for each line generate hypotheses,
  pick one, compute exceptions, mutate open balances (`book.apply`).
* **Why**: PARTIAL ("still open after this line"), DUPLICATE_PAYMENT ("already settled") and
  RETURNED ("re-opens") are properties of item *state*, not of a row. Verified: open-after ⇒
  PARTIAL/SHORT_DISPUTE at P 0.97 / R 0.995 once RETURNED is decided first.
* **Rejected**:
  - *Global optimisation (ILP / assignment over all lines)*: faster and "globally consistent",
    but destroys time order → loses PARTIAL/DUP/RETURNED.
  - *Two-pass "claims"* (first pass reserves items for strong-evidence lines, second pass
    stops weak matches from stealing them): proposed by the architect, killed by red-team —
    a duplicate payment's *first* payment is usually weak evidence (ar_ref_noise 2,820 / ar_1to1
    1,580) and the duplicate names the invoice; claims would give the invoice to the duplicate
    and destroy DUP detection. Kept as a measurable idea (gap bucket CONSUMED_EARLIER).
* **Can go wrong**: error cascade (an early wrong allocation changes the state seen by later
  lines). Mitigation: named items may fit against open OR original total within scorer tolerance.

## B2. Data model: integer cents, day ordinals, rejoin carry-over

* **Choice**: amounts as int cents parsed from strings (`money.py`, no floats); dates as
  `date.toordinal()`; item kind = item_id prefix; carried-over items (LIVE
  `open_items_at_start`) rejoined to TRAIN tables by item_id for terms/method/PO.
* **Why**: scorer tolerance is ±0.5%/1 cent — float drift flips boundary cases; the old engine
  subtracted YYYYMMDD integers (20250601 − 20250531 = 70, windows broke at month ends);
  stored kind as the literal "ARI"/"ARC" so 27.5k LIVE credit notes were never treated as
  credit notes; carried items had blank terms so SHORT_DISCOUNT could never fire on 325k items.
  All 325,419 carried ids exist in TRAIN tables (verified).
* **Rejected**: patching the old engine (user chose a rewrite; red-team argued porting was
  safer under time — trade-off accepted with `alt` as fallback).
* **Can go wrong**: FINAL's carried items may not exist in tables we have (they will exist in
  LIVE tables — loader must be pointed at LIVE for FINAL; **TODO verify when FINAL data lands**).
  Locale-ambiguous amounts ("1.023") raise instead of guessing — a parser crash is visible,
  a silent ×1000 error is not.

## B3. Text normalisation (`textviews.py`)

* **Choice**: fold accents/case; N43 fragments of exactly 38 chars followed by " | " are
  re-glued without space; tokens = alnum ref-like strings + glued-word splits + continuation lists.
* **Why**: N43 hard-wraps at 38 chars (85,528 fragments exactly 38 chars in TRAIN); names and
  invoice numbers are cut mid-word ("REDONDO JA | RDINES").
* **Can go wrong**: glued-word splitting emits a few junk tokens (e.g. "GRE250110905"); they
  only matter if they hit a real doc number, and the arithmetic gate filters most.

## B4. Treasury categories first (`treasury.py`)

* **Choice**: 4 literal regex families (CASH_POOL, INTERNAL_TRANSFER, FX_DEAL, INTERCOMPANY),
  run before any item search, final answer.
* **Why**: 19,244/19,250 TRAIN lines correct, **0 false positives** on 1.49M lines. The old
  engine looked for our own IBAN as counterparty — that field is empty on all 19,250 such lines
  (sample segments intercompany/internal_transfer were at 0.0).
* **Rejected**: bare keywords ("POOL", "PRESTAMO"): 81% of "POOL" lines on pool accounts are
  customers that build pools; "PRESTAMO" alone is also loan repayment.
* **Can go wrong**: new wording in FINAL (renamed group "Valtermia", new entity) → falls to the
  learned router, which is good but not perfect.

## B5. Operation files (`ops.py`) — strongest evidence

* **Choice**: DD batches, card settlements, cheque deposits, payment runs; indexed globally
  (all entity folders + 2–4 previous months from TRAIN); a line takes an operation only if
  |cash| equals its total, an e2e group, or an exact sub-sum, and the sign matches.
* **Why**: ids appear in text 100%; files list exact items. The holding (ES00) pays
  subsidiaries' suppliers but runs are filed under the subsidiary's folder (52k+39k+13k rows) —
  per-folder loading would lose ap_paid_by_holding (33k TRAIN lines). Factoring companies are
  paid instead of suppliers (37,812 run rows) → rows resolved by document number + amount,
  beneficiary ignored for the 4 factors. Fee lines reuse the batch id ("COMISION REMESA …
  125 RECIBOS" −18.75) → amount gate.
* **Measured**: sample exact item sets on dd 72/72, card 84/84, cheque 41/41, payment_multi
  787/787, single 458/458, cn_netted 97/97, factored 31/33, confirming 42/67.
* **Can go wrong**: large confirming remittances debited in slices (ap_confirming, 25 sample
  lines missed); subset search is capped (cost ~1,300 LIVE hypotheses of unknown quality).

## B6. Returns and re-payments (`returns.py`)

* **Choice**: return = opposite sign + return wording; link to original via mandate (latest
  collection ≤ date), cheque number, card last-4+merchant id (±1 month), or payment e2e;
  every item of the original gets RETURNED and is re-opened; a later payment from that party
  within 35 days goes to the re-opened item first.
* **Why**: FIFO would pick the re-presented item only 22% of the time; this rule 7,488/7,488.
* **Can go wrong**: same mandate + same amount in two collections (3 sample misses); bank
  reversals ("ANULACION OPERACION INDEBIDA") must not be treated as returns (excluded by words).

## B7. Remittance advices (`advices.py`)

* **Choice**: parse all 4 formats (CSV with , or ;, TXT e-mails, PDF, XLSX incl. the Sep-2025
  new layout); link an advice to a line by payment_date == booking_date and Σpaid == |cash|;
  advice rows give items and per-row reasons.
* **Why**: linkage 100% on TRAIN; docs == golden 99.97%; SHORT_DISPUTE exists **only** in
  advices (can't be inferred from arithmetic: 36,006 PARTIAL vs 2 SD on unexplained shorts);
  BR01 "RET" = withholding at gross (18,364/18,364). The old parser silently mis-read the new
  XLSX layout (voucher taken as invoice number) — affects LIVE and FINAL.
* **Can go wrong**: advices dated before the phase start are loaded from TRAIN folders (first
  run ~200 s, then cached); for FINAL the equivalent must come from LIVE folders — **verify**.
  Huge dispute lines (42–450 items) whose advice isn't found are the biggest remaining loss
  (ar_short_dispute ≈ 2 pts).

## B8. References (`resolve.py`)

* **Choice**: exact variants → PDF "replaces" map → suffix (prefix dropped) → prefix
  (truncated) → glued/continuation → 1-typo inside the payer only. Then arithmetic check;
  if it fails, try netting ≤3 open credit notes of the party.
* **Why**: bank drops our prefix far more often than it truncates; supplier numbers collide
  across suppliers (so AP is keyed by supplier too); fuzzy matching across parties produced
  false matches in the old engine.
* **Can go wrong**: ambiguous references with several valid credit-note combinations
  (ar_1toN_credit_note ≈ 1.45 pts lost).

## B9. Party + amount (`party.py`, `resolve.py`) — the biggest segment

* **Choice**: compute a **set** of candidate payers with sources in a fixed precedence:
  third-party markers (collection agency "EXPTE", confirming bank "ORD. … FRA … VTO",
  "POR CTA DE") replace the bank's counterparty → IBAN (masters + notice PDFs, old & new) →
  8-digit N43 ordering id learned from TRAIN → camt name → payer markers → trade name → name
  tokens. Expand to children of a parent payer. Then within the set: unique exact open amount;
  equal amounts → oldest due (or the month named in text → invoice of next month); else exact
  subset-sum. Exclude residues and items > 365 days old. **Never amount alone.**
* **Why**: ar_ref_noise (21% of TRAIN lines) has no reference but amount is unique within the
  payer 99.97%. Names are not keys (7,029 names shared by 23,393 customers in one entity) →
  sets, not first hit. 23% of unidentified receipts equal some open invoice → amount alone is
  a trap. Measured party recall 0.897 (0.964 on ar_ref_noise).
* **Rejected**: group-wide expansion (sibling companies paying: ~1 case in 1,298); dropping
  blocked customers (24,255 rows paid after the block date).
* **Can go wrong**: several exact subsets → picks a different but equally exact one
  (ar_ref_noise ≈ 0.6 pts); unknown payers in FINAL (new subsidiary ZA01 has no learned aliases).

## B10. Exception table (`decide.py`) — pure function

Order (first match per row; E7/E8 decided for the whole line):
E0 card order (gross, none) → E1 RETURNED → E2 DUPLICATE (AR ≤15 d, AP ≤50 d) → E3 credit
note (none) → E4 advice dispute → **E6 instalment by amount (before E5)** → E5 close /
OVERPAYMENT / FX → E7 discount (line identity) → E8 withholding (per-customer rate) →
E9 bank charges (country mismatch) → E10 PARTIAL.

* **Why each non-obvious choice**:
  - E6 before E5: the last instalment is identified by amount (last = total − (n−1)·⌊total/n⌋),
    not by "it closes the item" — 726 TRAIN rows close the item and are still PARTIAL.
  - E7/E8 line-level: 61% of discount rows and 74% of withholding rows sit on multi-invoice
    lines where a single row's shortfall is not observable; the line identity
    cash = Σ(total − deduction) − Σcredit notes holds 4,829/4,829.
  - E9 by country mismatch, not a SEPA list: Polish and Maltese payers are flagged in golden;
    rule precision 1,128/1,128.
  - Withholding rate per customer: 516 customers, none ever uses two rates; Spain-ES01 10% is
    usually a retention (PARTIAL), not withholding.
* **Measured (oracle: correct items given)**: F1 ≥ 0.95 every class; macro 0.995 with advices,
  0.81 without (SHORT_DISPUTE needs advices).
* **End-to-end sample**: macro-F1 0.752 — weakest OVERPAYMENT 0.39, SHORT_WITHHOLDING 0.55
  (8 cases), UNIDENTIFIED 0.47 (too many lines fall back to it). The gap between 0.995 and 0.752
  is matching, not the table.

## B11. Fallback category router (`cats.py`)

* **Choice**: learned table (format, bank code, subcode, sign, first two words) → category,
  with back-off for unseen banks; trained ≤ 2024-12; used only when no item evidence.
* **Why**: 0.9999 on 2025 category-only lines. But bank codes cannot tell "unidentified" from
  "customer payment" (95.6% vs 4.1% under the same code) → the router never vetoes an item.
* **Can go wrong**: a line with item evidence we failed to find becomes a category — e.g.
  ap_confirming lines classified CASH_POOL (0.55 pts).

## B12. Safety and determinism

* Writer guards (bad id, amount ≤ 0, duplicate pair, mixed rows, OVERPAYMENT/SBC/DUP on
  multi-item lines) demote the line to a category instead of risking a rejected file
  (the server rejects the **whole** file on one bad row).
* Deterministic: sorted iteration, no floats, work budgets counted in steps (not seconds).
  Two sample runs → same sha. LIVE determinism: **not yet checked**.
* Upload: only a human (`make push`, typed confirmation). Agents never submit.

## B13. Leakage check (is 87.66 honest?)

* Learned artefacts: router ≤2024-12, withholding rates ≤2024-12, aliases — first version
  learned on all TRAIN (includes the sample month). Re-run with aliases ≤2024-12: **87.66**
  (vs 87.70). Leak negligible.
* Sample opening balances are rebuilt by replaying TRAIN golden before June 2025. This is the
  equivalent of LIVE's official `open_items_at_start`, not a leak of sample answers. Without it:
  86.10.

## B14. Biggest remaining risks, ranked

1. **LIVE/FINAL not yet run and validated end-to-end** (runtime, memory, format) — must pass
   `bankrec validate` and a two-run determinism check before any upload.
2. **FINAL drift**: new entity ZA01 (no aliases, new numbering), masters_final, IT01 new
   account, Moncayo camt format — rules are mostly data-driven but unmeasured on FINAL.
3. **FINAL inputs that live in previous-phase folders** (advices, payment runs, returns of
   LIVE collections) — loader currently pulls from TRAIN; must pull from LIVE for FINAL.
4. **Over-caution** → UNIDENTIFIED too often (F1 0.47).
5. Built in ~2 hours: limited review. Old engine (78.65) remains the fallback.

## B15. Where the next points are (from `python -m recon2 gap --phase sample`)

ar_short_dispute (advice not found on 42–450-item lines) ≈ 2.0 · ar_1toN_credit_note ≈ 1.45 ·
ar_ref_noise (equal subsets) ≈ 0.6 · ap_confirming (slices + router) ≈ 0.55 · ar_n_to_m ≈ 0.4 ·
OVERPAYMENT / SHORT_WITHHOLDING / UNIDENTIFIED exception F1.
Each miss in `trace.jsonl.gz` carries a reason code; the gap report ranks them by points lost.
