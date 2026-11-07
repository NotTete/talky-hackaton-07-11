# recon2 — design v3 (accepted by the panel, 2026-11-07)

Outcome of a 2-round debate (ARCHITECT, ACCOUNTANT, TREASURY, DOCS, REDTEAM, VERIFIER).
Every rule below carries its TRAIN evidence. Raw evidence files: `docs/recon2/debate/`
(findings_*, brief_*, r1_*, r2_*, verify_batch1). Fallback engine = `bankrec.alt` (sample 78.65).

## 0. Contract
* `src/recon2/model.py` is frozen: Item, Line, Alloc, Hyp, Decision, `day()`. Int cents, day ordinals.
* Score: 0.5 allocation F1 (per row, amount ±max(0.01,0.5%)) + 0.3 all-or-nothing line accuracy
  + 0.2 exception macro-F1 (per ROW; classes absent from golden are free; TP ignores amount).
* Output is validated by the existing gate (`bankrec.builds`/`validate`); never upload (human gate).

## 1. Pipeline (single chronological pass; no two-pass claims — they break DUP)
```
load(phase) -> Lines (sorted day, txn), Items (+carry-over rejoin), Masters, FX
textviews    -> t_join / tokens per line
pre-route    -> treasury regex (category, final) -> returns/dup/refund pairing (T1)
resolvers    -> ops files (T0) | advices (T0) | refs (T2/T3) | party+amount / FIFO / month (T4/T5)
arbitrate    -> best fitting Hyp by (fit, tier, n_items, oldest, item ids)
decide()     -> per-row amount + exception (pure table §4), line-level E7/E8
book.apply   -> mutate open balances, hist, residue flags
writer       -> guards -> rows ; trace.jsonl
fallback     -> category router (learned bank-code/text map) ; credit -> UNIDENTIFIED/UNIDENTIFIED
```
Fit: same-ccy arithmetic EXACT (0c; ±1c only for %-rounding) — Σinv−Σcn == |cash| in 100% of clean
lines. Named items (tier ≤2) may fit against open OR total within scorer tolerance (avoid cascades).
Cross-ccy: item amount from `orig_amount` when orig_ccy == item ccy, else ECB booking-date rate with
one-sided spread (payments +0.15..+1.3%, receipts −2.1..−0.1%), clipped to 3%.
Never accept a hypothesis whose arithmetic fails. Never match on amount alone (23% of UNIDENTIFIED
receipts equal some open invoice). Deterministic work budget (no wall clock): fanout ≤250,
subset ≤40 items/≤8 picks/≤200k states; phase budget 50M states.

## 2. Loading facts (bugs of the old engine that must NOT be reproduced)
* `kind` = item_id prefix (open_items item_type ARI/ARC/API/APC). Old engine lost 27.5k LIVE CNs.
* Carried-over items (LIVE open_items_at_start, 325,419) all exist in TRAIN ar/ap/cn tables:
  rejoin terms, method, PO, party name, related_document by item_id. open = open_amount.
* Payment runs: index ALL folders by debtor_account_id/run_id/e2e (+ previous phase, 4-month
  lookback). ES00-paid runs are filed under ES01/ES02/PT01 folders.
* Entity from the line, never inner-join bank_accounts (IT01-VALD-EUR-2 missing; ZA01 in LIVE).
* Masters per phase (`masters_final` for FINAL). Dates as ordinals (old YYYYMMDD subtraction bug).
* N43 text wraps at 38 chars: a ` | ` after a 38-char fragment is a wrap → glue without space.

## 3. Resolvers (evidence → coverage)
* **Treasury regex (final category, before everything)** — 19,244/19,250 TRAIN, 0 FP:
  CASH_POOL `CASH CONCENTRATION|ZBA SWEEP TO HEADER|TRASPASO CASH POOLING|TRASPASO ENTRE CUENTAS ES00|TRASPASO PROPIO`;
  INTERNAL_TRANSFER `TRASPASO (A|DE) CTA \d{4}`; FX_DEAL `FX DEAL \d{10}|(COMPRA|VENTA) DIVISA [A-Z]{3} `;
  INTERCOMPANY `\bIC VALTER|IC VA$|PRESTAMO INTRAGRUP|DEVOLUCION PRESTAMO (IC )?VALTERM|TRANSF\. IC|FINANCIACION IC|PRESTAMO IC`
  (IC before LOAN). Never bare POOL/SWEEP/PRESTAMO/TRASPASO. Own-IBAN rule is dead (cpty acct NULL).
* **Ops files (T0)**: DD batch id in text 100%, |cash|==batch_total; cheque deposit id 100%;
  card (MID, date) Σnet==|cash|, orders cleared at GROSS (no exception); runs by run_id/e2e
  (100% multi/holding), rows→items by (scope entities, document_number, |document_amount|),
  negative document_amount = CN; beneficiary only tie-break, ignored for factors
  (Confirming Peñalara, Credifácil Comercial EFC, Ribagorza Confirming, Norte Pagos Confirming EFC).
  Gate: |cash| == op total or exact sub-sum and sign matches (fee line reuses batch id).
  AP scope: ES00 account → {ES00,ES01,ES02,PT01}; else own entity. AR: own entity only.
* **Returns (T1)**: sign opposite to item direction + return words; RETURNED on every item of the
  original op, amount = re-opened (total). DD by mandate → latest collection ≤ day; cheque number
  in cheque_deposits (535/538); card PAN4+MID+gross ±1 month files (73% same month);
  AP rejected transfer by e2e incl. netted APC rows. Re-payment after return: reopened item wins
  over FIFO for 35 days (100% within 35d; FIFO right only 22%).
* **Duplicates (T1)**: item already settled, same amount, AR ≤15d / AP ≤50d, 1-item line →
  DUPLICATE_PAYMENT (AR: 99.98% no same-amount alternative). Ref to a CLOSED item → DUP, never a
  FIFO sibling. Refund pairing: debit == earlier AR DUP same account ≤47d → CUSTOMER_REFUND
  (2,562/2,611); credit == earlier AP DUP ≤50d → SUPPLIER_REFUND (479/483); else credit == open APC
  → settle APC.
* **Advices (T0)**: link on payment_date == booking_date (100%) and Σpaid == |cash| (incl. negative
  CN rows), same ccy; payment_ref token tie-break. Rows → items by doc (suffix within payer only).
  Reason COOP MKT/PRICE DIFF/LOGISTIC FEE/SHORT DELIVERY/PENAL. RETRASO/DAMAGED GOODS → SHORT_DISPUTE
  on that row (amount=paid). Reason RET (BR01) → SHORT_WITHHOLDING, amount=GROSS (18,364/18,364).
* **Refs (T2/T3)**: exact (alnum, digit core, no leading zeros, PO) → PDF replaces → suffix (dropped
  prefix) → prefix (truncation 16/35/38) → glued/continuation lists → 1-typo within party only.
* **Party + amount (T4/T5)** — ar_ref_noise (21% of lines) is 99.97% unique within party:
  party SET with precedence: third-party marker REPLACES counterparty (EXPTE n <debtor> <suffix>;
  `ORD. <debtor> FRA <doc split> VTO ddmmyy`; `POR CTA DE <child>`) > IBAN (masters ∪ notice
  old/new; exclude `..00ZZZ` creditor ids) > N43 8-digit ordering id alias (from TRAIN) > camt
  counterparty_name > payer markers on t_join > trade_name > folded name → SET (names are not keys).
  Expand children(payer) via parent_customer_id (1,157/1,298); NOT group_id. Don't filter blocked.
  Unique open amount in party → T4; several equal → oldest due (age proxy: skip >365d) or month
  named in text M → invoice issued M+1 (100%); exact subset-sum (+≤3 CNs). Residues excluded.
* **n_to_m (subset + one partial)**: only for NAMED items (ref list/advice); full rows first; the
  single PARTIAL goes to the last named item (99.15% when all docs present), else latest due.
* **Category router** (no item evidence): learned (fmt, code, subcode, sign, first-2 masked tokens)
  → category with back-off; overrides PROFORMA credit → CUSTOMER_ADVANCE; refund pairing above;
  interest by sign. Credit without evidence → UNIDENTIFIED + exception UNIDENTIFIED.

## 4. decide() — pure, per line (E-rules first match wins per row)
Inputs: item state (total, open_before, hist, plan), line (cash, day, ccy), hyp (advice facts,
partial_item, is_return), masters (terms, customer country, entity country), A3 withholding rates.
* E0 card order → '' amount = gross.
* E1 return → RETURNED, amount = re-opened (item total / earlier applied); includes APC rows.
* E2 already settled, same amount, window (AR 15d, AP 50d), 1-item → DUPLICATE_PAYMENT, amount=cash.
* E3 ARC/APC netted → '' amount = open_before (cross-ccy CN also '').
* E4 advice dispute reason → SHORT_DISPUTE, amount = paid (beats FX/PARTIAL). One row per line.
* E6 (BEFORE E5) instalment plan known (PDF n, or repeated piece; n = round(total/first_piece) 100%
  on paid plans): piece = floor(total/n), last = total−(n−1)·piece; applied == last ±1c → ''
  (6,329/6,329); any other piece → PARTIAL even if it closes the item (726 cases). Retention 0.95/0.90
  first payment → PARTIAL even if the release was booked earlier.
* E5 applied ≥ open_before: ARI & 1 item & first payment & same ccy & excess → OVERPAYMENT
  (amount=open); else cross-ccy → FX_DIFFERENCE; else ''. (last instalment, release, remainder,
  re-presentation all '').
* E7 SHORT_DISCOUNT, LINE-LEVEL: terms pct>0, item untouched (open==total), booking ≤ issue +
  disc_days + 3, discount = round_half_up(pct·total); line identity cash == Σ(total − disc on in-window
  rows) + Σ(other rows) − ΣCN (4,829/4,829). Late → PARTIAL (3,232, clean split). FX if cross.
* E8 SHORT_WITHHOLDING, LINE-LEVEL: r from A3[customer] (fixed per customer, 516) else entity default
  BR/MA {1.5%,10%}, MX {1.25%,10%}; ES01 only for MA customers (else 10% = retention → PARTIAL);
  cash == Σ(total − round(r·total)) − ΣCN; 74% of SW rows are on multi-item lines. FX if cross.
* E9 SHORT_BANK_CHARGES: ARI, 1 item, first payment, customer.country ≠ entity.country,
  0 < short ≤ 71.10 and ≤ 3.2% total, not discount-shaped → amount = total (1,128/1,128, 100%).
* E10 else PARTIAL, amount = applied (cross-ccy keeps PARTIAL).
* Only ARI gets OVERPAYMENT/SHORT_*; API gets ''/PARTIAL/FX/RETURNED/DUP; ARC always ''.
Writer guards: item_id regex, amount > 0, no duplicate (line,item), no OVERPAYMENT/SBC/DUP on
multi-item lines, no mixed rows → violating line demoted to category + trace reason.

## 5. Residues
Item with 0 < open < total after a PARTIAL/SHORT_DISPUTE, or carried with open < total
(LIVE 30,827; 9,975 are 2% late-discount residue paid only 2.4%). Excluded from amount-unique and
FIFO; reachable by explicit ref, advice, release words (RET. GARANTIA, RETENTION RELEASE,
LIBERACION RETENCION, SETTLEMENT <doc>, RESTANTE, FREIGABE EINBEHALT), re-presentation, n_to_m.

## 6. Trace (for the next fixing agent) — trace.jsonl, one record per line, sorted keys
`{txn, route, resolver, tier, parties:[[id,src]], cands:[[item_id,resolver,reason]]≤10,
rows:[[item_id,amt,exc,rule]], category, reason, caps:[...]}`. Reason codes (closed):
NO_PARTY, PARTY_AMBIGUOUS, NO_REF, REF_NOT_FOUND, REF_AMBIGUOUS, OP_NOT_FOUND, OP_FEE_LINE,
ADVICE_NOT_FOUND, ADVICE_ROW_UNRESOLVED, AMOUNT_NO_FIT, FX_OUT_OF_BAND, ITEM_CLOSED, ITEM_STALE,
RESIDUE_BLOCKED, AMOUNT_ONLY_FORBIDDEN, FANOUT_CAP, SUBSET_CAP, GUARD_DEMOTED, TREASURY, ROUTER.
`python -m recon2 gap --phase sample` joins trace × golden and buckets every miss
(NOT_GENERATED / REJECTED(reason) / CHOSEN_OTHER / AMOUNT_OFF(rule) / EXC_WRONG(rule) /
EXTRA_FROM / WRONG_CAT / ITEM_EXPECTED) ranked by points lost with 3 examples each.

## 7. Dev loop (fast)
* `python -m recon2 run --phase sample` end-to-end (8.5k lines, target < 60 s) + vendored scorer.
* Oracle harness `python -m recon2.dev.oracle` on the VERIFIER sample
  (a stratified TRAIN sample, 16k lines stratified ≥50/segment, true open balances) → decide()
  confusion per exception class in < 5 s; resolver P/R per segment.
* Sample has no ES00/treasury/BR-MX-MA: check those segments on the oracle sample.

## 8. Work packages (parallel, disjoint files; tests in tests/recon2/)
| WP | files | owner |
|---|---|---|
| A data | money.py, textviews.py, fx.py, load.py, book.py | builder-data |
| B docs | advices.py, pdfclauses.py | builder-docs |
| C routing | treasury.py, cats.py, party.py (+ learned artefacts in artifacts/) | builder-routing |
| D accounting | decide.py, dev/oracle.py | builder-accounting |
| E ops | ops.py, returns.py | builder-ops |
| F integration | resolve.py, engine.py, writer.py, trace.py, gap.py, cli/__main__.py | integrator |
Go/no-go at the freeze: recon2 ships only if it beats alt on sample AND oracle segments, validates,
and is deterministic (two runs, same decompressed sha256). Otherwise FINAL ships alt.
