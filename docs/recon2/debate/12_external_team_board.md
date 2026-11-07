# Board on an independent team's engine ("bounded", examples_matcher)

An independent team's source code was reviewed by a two-member board (learn + validate). Their code was
treated as untrusted: read, and run only on the sample, never with network access or tokens.

## Learn board
* The team had captured the organisers' official worked examples page (one example per segment) — the
  closest thing to a written rulebook. **No hard violations** found in our engine.
* Items worth checking: dispute reasons (any stated deduction other than a discount or withholding is a
  dispute — our whitelist was narrower → broadened); withholding flags every invoice row of the line at
  gross; duplicates found by payer name + exact amount against an already-closed item.
* Techniques worth porting: a per-item date window [issue − 7, due + 183] (their measured coverage
  99.49 %); the ES00 entity tree; "full set + one partial" for n-to-m; do not accept OVERPAYMENT when an
  exact item exists for another candidate payer; late-discount PARTIAL from the payer pool; the cheque
  regex.

## Validate board
* Reproduced their claimed 86.36 as **86.03** (vendored scorer, sample). Ours on the same scorer: 90.16.
* Leakage audit: no golden reads at match time; their own report admits patterns were written while
  looking at the sample (tuned, not leaked).
* Per segment, they beat us on: ap_supplier_refund (100 vs 36.8), ar_advance_deduction (91.7 vs 41.7),
  ar_short_withholding (100 vs 60), ar_dd_returned (97.0 vs 87.6), ar_dd_batch (100 vs 90.3),
  ar_short_bank_charges (100 vs 87.5), unidentified (99.3 vs 93.4).
* We beat them widely on ap_confirming, ar_n_to_m, ar_overpayment, ar_1toN_remittance, cheques,
  chargebacks, AP duplicates/returns, credit-note netting, partials, refunds, ap_payment_multi; overall
  precision 0.986 vs 0.91.

## What we ported (port fixer, each gated on the sample)
Supplier refund via the opposite-side party set (+0.35); returned DD via the customer's mandate when no
batch row explains the cash (+0.24, 199 / 202); advance deduction from the PDF "net amount due" (12 / 12);
per-customer withholding rates incl. ES01 Moroccan customers (+0.92, 5 / 5); bank-charge disambiguation
by amount among a payer's items sharing a truncated reference (8 / 8); a tolerance-only unique amount
across several payers no longer emitted; the CUSTOMER_REFUND router guard. Sample 90.16 → **91.89**,
then 92.04 after a crash fix.
