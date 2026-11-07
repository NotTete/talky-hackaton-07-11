# ROUND 1 — ACCOUNTANT critique of v1: ACCEPT-WITH-CHANGES
BLOCKERS
B1 E4 "unexplained deduction -> SHORT_DISPUTE" WRONG: 1-item first payments cash<open: 36,006 PARTIAL vs 2 SD. SD only from advice reason, exactly one row/line. Unexplained short = PARTIAL.
B2 Instalments missing: last piece by AMOUNT (piece=floor(total/n), last=total-(n-1)piece): ==last 6,329/6,329 empty; others 15,681 PARTIAL vs 138 empty incl 726 that close the item. Add E6 plan piece; n from PDF else total/first piece ≈ int 2..6. Retention: 20 releases before 0.95 payment, still PARTIAL.
B3 SBC bounds ok (47/1,130 >2%, 264 >1%, all single-ccy US01/AU01/ES01 USD/CAD/AUD -> use <=3.2%) BUT gate on customers.country != entities.country: 1,083 SBC vs 14 PARTIAL (98.7%) excl discount shape; zero empty. Within v1 bounds 2,547 1-item first-pay PARTIAL (2,520 ar_discount_late) -> E7 must run first and "late->PARTIAL" final. A4 keyed on payer vs entity country, /CHGS/ bonus.
B4 Withholding: ES01 10% shorts 302 PARTIAL (retention) vs 42 SW (Moroccan customers). BR/MA 1.5|10, MX 1.25|10 never 1.5. Rate fixed per customer (516, 0 with two). A3 party->rate primary; country default only for unseen customers with entity-specific rates.
B5 RESIDUES treated as normal open items: late-discount 2% paid 2.4%; advance 0/2,003; retention 60% ~339d; dispute 33% median 55 max 92. LIVE 30,827 partially open ARI (9,975 at 0.02). Flag residue items; exclude from amount-unique & FIFO; allow only via explicit ref, advice, release keyword ("RET. GARANTIA","RETENTION RELEASE","SETTLEMENT <doc>","RESTANTE") or n_to_m remainder. Discount/WHT arithmetic on TOTAL only when open_before==total.
B6 Fit can't express ar_n_to_m (13,904 lines; 6,937 exactly one PARTIAL row, never two; 6,824 contain carried remainder). Add "subset + one partial" fit when items are NAMED (ref/advice/text list): full rows first, residual to exactly one named open item, residual<open -> PARTIAL. Partial item latest-due only 56% (VERIFY selector).
MAJOR
M1 DUP: AR same amount, gap<=15d (median 7), same account 94.6%, k=1. AP duplicate repeats first payment text, no e2e, 14-50d; 17/484 repeat a PARTIAL piece (item not CLOSED) -> E2 = same amount as earlier allocation of this item within 50d and item has no open need. R-REF T2 must check state: ref to CLOSED item -> DUP, never FIFO sibling.
M2 CUSTOMER_REFUND pairing with earlier AR DUP same account <=47d (2,562/2,611, unique 2,559) as T1 category rule + keyword (R 96.2%). SUPPLIER_REFUND by AP dup 479/483 within 50d.
M3 Chargebacks PAN4+MID+gross unique only 212/289 -> search ±1 month settlement files; amount = order gross.
M4 Card orders: 645 one-order settlements cash<total -> E10 on gross allocation (applied=gross=total -> empty).
M5 Returned AP transfers include netted APC rows (64 positive RETURNED), cash = API − APC; E1 covers APC; fit uses netting.
M6 Transfer after DD return -> reopened item 7,488/7,488 (FIFO 7%), median 18d max 33. R-REP outranks R-PTY FIFO for that party for 35 days; "PAGO RECIBO" optional.
M7 R-PART fractions: drop 0.98 (late discount E7), 0.95/0.90 (retention via PDF/release). Instalment 1/3 9,230, 1/4 6,764, 1/6 3,981, 1/2 2,294. ar_partial first pieces ~U(0.3,0.75) weak -> require ref.
M8 Discount window exact: base total, half-up, booking_date, <= disc_days+3: 38,854 vs 3,232, 0 overlap; multi-invoice per row 4,829/4,829.
MINOR m1 OVERPAYMENT only 1-item first payment (assert). m2 ARC related_document same line 50% -> tie-break only; 1,132 rappels none. m3 advice reason "RET" (~18k rows) unspecified. m4 assert no OVERPAYMENT/SBC/DUP on multi-item lines in writer.
DEV LOOP
D1 Oracle-state unit loop: for each line in verifier stratified sample, seed book at its date from golden (open = total − Σgolden before line, status, history = V2 synthesis per line); run only resolvers + excepts; score per segment with vendored scorer restricted to those lines. Isolates generation/exception bugs from cascades. <30s.
D2 Exception unit table <1s: pure decide() over golden rows -> per-class confusion matrix (E-rules developed without matching; 50% of macro-F1).
D3 H25 full chronological only at milestones; ratchet on D1 per-segment table.
VERIFY V1 advice "RET" rows: BR/MA/MX = SW, else PARTIAL. V2 >=95% AR DUP lines: party has no other open item same amount at that date. V3 round(total/first_piece)∈{2..6} gives right n >=95% of ar_instalment. V4 n_to_m PARTIAL item = last listed in text/advice vs latest issue. V5 LIVE carried open/total ∈{0.05,0.10} have PDF retention clause.
