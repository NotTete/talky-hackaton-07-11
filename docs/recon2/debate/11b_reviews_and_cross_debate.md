# Review round after the build — code reviewer vs accountant reviewer (with cross-critique)

The first round of code review is in `11a_review_code_round1.md`. This file records the rest of the
exchange, in order.

## A. Code reviewer — round 2: why does AP collapse on the TRAIN window?
Evidence: train:2025-05 scored 82.08; AP segments collapsed (ap_payment_multi 714 wrong lines,
ap_paid_by_holding 685, ap_payment_single 361). Diagnosis (trace × golden × bank text):
* **Not** the "all TRAIN op files loaded" hypothesis and not entity scope: a real ops bug that also hits
  LIVE.
* Spanish N43 payment-run debits — `PAGO TRANSFERENCIAS | 3023-56-2440945-24 376128 000046`, ref2
  `PR010125052693-0` — truncate the end-to-end id; the run file key is `PR010125052693-0046`. The
  sequence is the trailing 6-digit token of the text. No full e2e token exists, so the whole run (~700
  rows) was subset-summed and failed; the line fell to the router.
* LIVE exposure: `PAGO TRANSFERENCIAS` appears 7,431 times (~3.6 % of lines) → ~3–4 points.
* Exact fix: rebuild `<run>-<seq:04d>` from ref2 + the last 6-digit token and look it up first.
* Smaller: ap_supplier_refund 255 / 271 wrong with no party on ACH text; window artefacts from old items.

## B. Accountant reviewer — gap analysis
* ar_short_dispute: in 17 of 25 wrong lines **no advice exists at all**; golden = the payer's oldest-due
  open book (55 % gap-free prefixes, the rest 1–4 gaps), cash = Σinv − ΣCN exactly.
* ar_1toN_credit_note / ar_ref_noise: the same sweep idea at smaller sizes; wrong picks = arbitrary
  equal-sum subsets where a due-order prefix exists.
* **OVERPAYMENT F1 0.39 is false positives**: 18 of 29 came from E5 labelling OVERPAYMENT whenever a
  0.5 % tolerance fit gave a surplus (0.14, 0.18, 2.76 …); every true one has a surplus in
  {1, 2, 5, 10, 50, 100, 1 %, 10 %} — perfect separation.
* SHORT_WITHHOLDING: missed lines are 0.90 × a subset of one payer's invoices; the router called them
  FX_DEAL.
* UNIDENTIFIED F1 0.54 is a precision problem: 366 false UNIDENTIFIED exceptions where a strong payer
  had candidates; only 25 true UNIDENTIFIED share that shape.
* The TRAIN router bucket (1,786 lines, 4.87 points) is almost all N43 payment-run lines — independently
  the same root cause as the code reviewer found.

Fix list (ranked): N43 e2e rebuild; payer sweep prefix with credit-note and dispute slack; OVERPAYMENT
shape both ways; withholding cash/0.9 subsets; UNIDENTIFIED discipline; router labels; wider credit-note
netting.

## C. Cross-critique (each reviewer on the other)
* Accountant on code: agrees with the sample-carry leak (HIGH 3), the alias leak (HIGH 4), silent stubs
  (HIGH 2) and the missing coverage assert (LOW 9); adds two issues missing from the code review: the N43
  e2e suffix and the tolerance-only OVERPAYMENT false positives.
* Code reviewer's own top-5: fail loudly on LIVE/FINAL; FINAL carry-over rejoin from LIVE tables;
  sample carry strictly by date; holdout aliases for every golden-scored number; coverage assert,
  hash-seed determinism, loud handling of a missing ZAR rate.

## D. Consensus and what was done
Both reviewers converged on the N43 e2e rebuild as the top fix (assigned to the ops owner). The fixer
then implemented, in order and each gated on the sample: OVERPAYMENT shape (+1.14), UNIDENTIFIED
discipline (+0.48), withholding subsets (+0.00 on the sample, kept), party sweep (+0.89), child-party
gate (−0.02, kept because children were wrong 9 / 12 on TRAIN) → sample 87.66 → **90.15**, TRAIN May
82.08 → **86.87**. The leak fixes (FINAL carry rejoin, carry by date, holdout aliases) left the sample
hash unchanged — the honest baseline became 87.66.
