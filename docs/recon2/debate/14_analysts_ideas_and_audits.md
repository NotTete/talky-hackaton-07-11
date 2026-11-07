# Analysts, idea boards and audits (late phase)

## Analyst — LIVE UNIDENTIFIED excess
17,126 LIVE lines (8.4 %) ended UNIDENTIFIED against ~2.3 % expected from TRAIN. All credits; US01 5,540,
ES01 2,233, AU01 1,158 … ; camt 8,798, MT940 5,826, N43 2,502. A payer was found on 11.5k of them.
Amount vs the payer's items: single item ~1.1k; sum of 2 items 3,164; a fraction of one item (partial /
instalment) 3,179; 3+ item subsets 3,555. Top patterns: US "ACH CREDIT <name> CCD PAYMENT ID" (3,610),
initials-only payers (867), AU "DIRECT CREDIT … INSTALMENT k/n" (733), German SEPA with empty purpose,
and Spanish/Mexican/Brazilian/Polish/French/Italian transfers with no payer. Conclusion: mostly
known-payer receipts covering several invoices or a partial — a subset problem, not an amount-lookup bug.

## Analyst — large-line sweeps
ar_short_dispute: every disputed item's remainder is exactly 3/5/10/20/30 % of its total (213 / 213);
credit notes per line are common. ar_n_to_m: one disputed item per line, remainder 40/50/60 %.
ar_1toN_credit_note: almost no deductions, usually one credit note, ~45 % exact due-order prefixes.
Spec: a "dispute subset" solver (prefix + credit notes + ≤3 disputed items with remainders from the
percentage set, unique solution only). Not shipped (time risk); confirmed later by the official
examples (section 9 of the report).

## Fixer — UNIDENTIFIED leak buckets (TRAIN May, 1,753 leaked lines)
425 no party; 362 golden items were part-paid residues (excluded by design); ~540 large sweeps (3–66
items) with many equal-sum subsets; 114 items already closed by an earlier wrong line; ~128 single-item
partial shapes (30–70 %, 98 %); 16 single items older than 365 days; ~120 sums not matching
(credit-note / deduction mixes). Shipped: part-paid remainders, partial shapes, old single items,
causality filter → TRAIN May 87.88 → **88.98**, sample 92.66.

## Ideas board (high-capability model, strict token budget)
Ideas: same-phase alias bootstrap (+1.0–1.5, shipped as the payer bootstrap); PO / advice numbers as
reference keys (already indexed); month bucketing for payers over the candidate cap; wider credit-note
netting when unique; confirming routing before treasury (shipped as a guard); payer-scoped one-character
reference fuzz. FINAL risks: renamed customers, missing ZAR rate (fixed: refuse to convert), LIVE roots
for FINAL (already in place), strict-mode abort leaving no file (fixed: escape hatch).

## Common-sense rules audit (senior AR/AP accountant)
Most practitioner rules were already implemented (direction, entity scope, reversals, payroll / tax
timing). Missing and worth adding: **causality** (never pay an item before its issue date — 0 violations
in 5,155,603 TRAIN rows; shipped) and a per-payer payment-lag prior (within-party lag sd 13.4 d vs 33.4 d
overall; queued). Debunked: "partials are round numbers" — 55 % of partials are a multiple of 10 % of the
total, < 0.01 % are round to 100 / 1,000.

## Exception audit (per class, sample, v4)
PARTIAL 0.858, OVERPAYMENT 0.979, SHORT_DISCOUNT 0.979, SHORT_BANK_CHARGES 0.900, SHORT_WITHHOLDING
1.000, SHORT_DISPUTE 0.774, DUPLICATE_PAYMENT 0.721, RETURNED 0.986, FX_DIFFERENCE 0.958. Nearly every
miss is a missing item (UNIDENTIFIED line), not a wrong code. Shipped: late discount on the unique 1–3
discount-terms subset that explains the shortfall (sample 92.18 → 92.23).

## Char n-gram payer matcher
Held-out TRAIN March 2025 (18,958 lines): top-1 0.442, top-5 0.592 (many customers share names); with a
gap rule (score ≥ 0.8 and 0.1 ahead of the runner-up): **99.1 % precision at 26.9 % coverage**. On the
1,543 lines where we find no payer it rescues only 99 (6.4 %, 100 % precise) — those lines carry no
payer name resembling any customer. Built and committed, not wired (≈ +0.1).

## Reference-extraction audit
Visible-reference recall 90.3 % → **99.7 %** after joining slash-numbered documents (749 of 772 misses).

## Advice-chain audit
327 / 327 advice-linked sample lines give the exact golden item set (CSV 120, PDF 122, TXT 74, XLSX 11).

## Overfitting check
Held-out TRAIN February 2025 (never tuned on): **92.75** with v6 (May 91.51) — the rules generalise.
UNIDENTIFIED-exception rule re-scored on three windows: keep (sample 92.66 vs 92.37 without it).

## Model-based alternatives considered
* A small LLM "decision model" (Kev-0.8B) — zero-shot ~0.65–0.70 on its own benchmarks vs our router
  0.9999; one forward pass per line is too slow for 200k lines on CPU; it does not solve item selection
  (arithmetic/search) and gives no similarity output for payer matching. Rejected for this event.
* JEPA-style learned representations for extraction — extraction is already at 99.7 % / 100 % on
  template-generated documents; a model adds noise and non-determinism. Rejected.
