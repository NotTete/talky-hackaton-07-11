# Existing engine — facts from SOURCE CODE (src/bankrec/, HEAD + working tree). Paths relative to src/bankrec/.

## Summary
1. Single chronological pass (booking_date, bank_txn_id) with mutable Ledger (open/paid). 5 DOCUMENT steps first-hit-wins: returned -> operation_id -> advice_ref -> operation_total -> advice_party; then 4 INFERENCE steps (reference, advice_total, party_sweep, party_amount) arbitrated by |net-cash|<=1%, and if none fits the smallest-gap proposal is ACCEPTED anyway; then supplier_refund; then category (engine.py:116-209).
2. LIVE BUG: open_items_at_start kind = literal item_type "ARI"/"ARC"... (data.py:330) so every kind=="credit_note" check misses 27.5k carried-over LIVE credit notes.
3. LIVE BUG: carried-over items (325k) have blank payment_terms/method/PO (data.py:340-346) -> SHORT_DISCOUNT can't fire, card/DD items sweepable. open_items has 0 overlap with live ar/ap/cn tables; could rejoin TRAIN invoices by item_id.
4. Date windows subtract YYYYMMDD ints (ops.py:235 <=5, advice.py:397 <=25, :407 <=21) -> break across month boundaries.
5. config/bank_codes.json missing -> bank_code/subcode never used; categories via own-IBAN + keyword list only.
6. Tolerances: match 1% (min 2c), FX 3%, op/advice totals 0.1%, ops.select 0.5%; gross-up rates 10/3/2/1.5/1.25/1% at 0.1%.
7. Caps: MAX_FANOUT 400; prefix>=7 suffix>=6 scan<=64 keys; subset-sum 400 groups/200k states/6 picks; party_amount is FIFO prefix-sum not true subset-sum (engine.py:993).
8. Ref resolution: exact (alnum/digit-core/no leading zeros + PO) -> PDF replaces -> suffix (dropped prefix "FRAS 0414882") -> scope digit endswith -> 1-typo/swap in scope -> prefix (truncation 16/35/38). Continuation tails "RE250106232 106234 ..." and glued "RECHNUNGRE250110905" expanded.
9. Exceptions (excepts.py): close -> ""/FX; excess -> OVERPAYMENT (1-item lines); discount; withholding (10/1.5/1.25, passes if country unknown); bank charges (payer country != entity country, <=2%, <=60EUR, != discount); any advice reason -> SHORT_DISPUTE; else PARTIAL. DUP/RETURNED/UNIDENTIFIED in engine.
10. Parsed but UNUSED: PDF clauses assignment/factoring (sample 407/live 1497), iban_change, bank_account_notice, advance, rappel, service_month; credit_notes.related_document; customers parent_customer_id/group_id/trade_name/sepa_mandate_id; dd re_presentation; card fee; statement_format drift; value_date.
11. Party detection: master IBAN wins; else counterparty_name -> text after payer marker -> full text, FIRST source with any party wins (wrong counterparty_name hides real payer).
12. DUPLICATE detection only vs items paid earlier in same run (pre-phase duplicates in LIVE undetectable).
13. preprocess.py/documents.py stale; LineDecision reason/candidates always empty.
14. Sample (74.33) zero-accuracy segments: ap_duplicate_payment, ap_duplicate_refund, ap_returned_transfer, ar_card_chargeback, ar_cheque_returned, customer_refund(20 lines), nonitem_card_expenses, nonitem_intercompany, nonitem_interest_income, nonitem_internal_transfer.
15. Biggest losses (lines x (1-acc)): ar_ref_noise ~612 (2155 lines, .716), ar_1toN_text ~123, ar_1toN_credit_note ~122, ar_partial ~122, ar_n_to_m ~92, ap_direct_debit ~87, unidentified ~86, ap_payment_multi ~60, ar_same_amount_month ~59, ar_instalment ~57, ar_dispute_settlement ~52, ar_same_amount_fifo ~50, ap_confirming ~45, unidentified_lookalike ~44.

## Facts
- LIVE: 203,083 lines (camt053 105,538 / mt940 59,565 / n43 37,980). open_items_at_start 325,419 (ARI 229,534, ARC 25,434, API 68,356, APC 2,095), issued 2021-03..2025-06; live ar/ap/cn 706,157 new items 2025-07..12.
- Sample golden 8,499 lines / 36,574 rows: none 34,527, FX 605, PARTIAL 466, SHORT_DISCOUNT 247, SHORT_DISPUTE 225, UNIDENTIFIED 218, RETURNED 210, DUP 35, OVERPAYMENT 24, SHORT_BANK_CHARGES 9, SHORT_WITHHOLDING 8.
- Loading: CSV as strings; to_cents float*100 round (data.py:41-52); dates ISO strings -> YYYYMMDD ints. text.parse_amount handles "1.210,00", "1,210.00". pdf/fields.money last-separator; "1,752.08" -> 175208.
- bank_transactions unused cols: value_date, bank, statement_format, statement_file. bank_statements/ raw never read. masters/entities.csv not read (entity->country hardcoded excepts.py:226-230: ES00/01/02 ES, PT01 PT, FR01 FR, IT01 IT, DE01 DE, NL01 NL, UK01 GB, PL01 PL, CH01 CH, US01 US, MX01 MX, BR01 BR, MA01 MA, CN01 CN, AU01 AU, ZA01 ZA).
- FX: last rate <= date, convert = cents/src*dst.
- payment_runs: this phase + 4-month lookback into other phases (data.py:358-390); run_type ignored.
- Card: op total = sum(net), orders cleared at gross; per-order ops absent -> chargebacks unresolvable.
- Advice parsers (alt/advice.py): CSV (metadata row payment_ref/payment_date; header document/documento/beleg/facture; cols gross/importe/brutto/montant, paid/pagado/net/zahlbetrag, discount_or_deduction/descuento/abzug, reason/motivo/grund/motif; "total" row); TXT (payment_ref first >=8-digit number on Subject; "payment of X CCY"; "on dd/mm/yy"; rows split 2+ spaces); PDF via pypdf one cell per line ("PAYMENT REF","DATE","AMOUNT"; "-60.11 SHORT DELIVERY"); XLSX via zipfile/ElementTree. Keys: alnum(payment_ref) and filename tail (RA_20250601_PARDOPOOLTEC_520628.txt -> 520628).
- Sample PDF index: invoice 1136, supplier_invoice 999, supplier_credit_note 105, credit_note 32, bank_account_notice 16, proforma 10. Advices: csv 123, pdf 123, txt 73, xlsx 12.
- Context per line: tokens from text/ref1/ref2/remittance_info/e2e (+counterparty_name), folded upper no accents. If orig_amount/orig_currency present, cash/currency replaced. want_count regex `\b(\d{1,4})\s+(?:PROVEEDORES|RECIBOS|POSTEN|OPERATIONS|OPERACIONES|ITEMS|EFFETS)`.
- is_return: RETURN word (DEVOL, DEVUELT, IMPAYE, RETOUR, RUECKLASTSCHRIFT, RETURNED, CHARGEBACK, CONTRACARGO, REJECT, RECHAZ, UNPAID, R-TRANSACTION, RIMBORSO, RETROCESSION, STORNO) and not REVERSAL (OPERACION INDEBIDA, ANULACION OPERACION, BANK ERROR) and not GARANTIA/RETENCION/RETENTION and (cash<0 or "TRANSFERENCIA DEVUELTA"/"RETURNED TRANSFER").
- Payer markers: "TRANSFERENCIA DE ", "TRANSFERENCIAS ", "ORD. ", "ORDENANTE ", " AN ", "/ORDP/", "ABWA+", "VIREMENT DE ", "VON ", "DE LA PART DE ".
- ops.select: tol max(2c,0.5%): whole op -> all; else single entry; else BFS subset-sum by e2e/beneficiary groups up to want_count or 6, rejects ambiguous sums.
- operation_total: account_id exact, |dYMD|<=5, unique, tol min(1%,max(2,0.1%)).
- advice_party: payer_name -> up to 3 parties, received within 21, closest total.
- _share: credit notes full open, target=cash+notes; if invoices sum to target (or grossed-up) split pro rata; 1 invoice gets whole target; else each full open.
- Unnamed credit notes: >=2 named invoices, one party, named total exceeds cash -> search party's first 8 open CNs for unique 1-3 combo equal to overshoot.
- DUPLICATE only when line has exactly one item, item settled in this run, within 1%.
- Category keyword lists (cats.py), sign rules: credit-only CUSTOMER_ADVANCE, SUPPLIER_REFUND, TAX_REFUND, INTEREST_INCOME, LOAN_DRAWDOWN, UNIDENTIFIED; debit-only CUSTOMER_REFUND, PAYROLL, SOCIAL_SECURITY, TAX_PAYMENT, LEASING, INSURANCE, LOAN_REPAYMENT, INTEREST_EXPENSE, CARD_EXPENSES. Own-IBAN: pool account + POOL/BARRIDO/SWEEP/ZABA/NIVELACION -> CASH_POOL; same entity -> INTERNAL_TRANSFER; else INTERCOMPANY. Fallback credit UNIDENTIFIED, debit BANK_FEES.
- Ledger indexes: _by_doc (variants+PO), _by_party (oldest first), _by_amount (on total_cents, NOT open balance — bug), sorted keys prefix bisect, reversed keys suffix bisect, party token index (tokens>=4, similarity>=0.6, <=6 parties).

## Hard-won bank text patterns (literal)
- `SEPA-UEBERWEISUNG EREF+NOTPROVIDED SVWZ+RECHNUNG RE250110905 ABWA+Pooltechnik Weißenau GmbH & Co. KG` with counterparty_name "VMW Wellness GmbH" (different party!) — golden SHORT_DISCOUNT.
- `SVWZ+RE RE250106232 106234 106402 106390 106918 105438 107372` continuation lists.
- Truncation: `FRAS FV25-02`, `PAGO FRA. FV25-0`. Prefix dropped: `FRAS 0414882` (FV25-0414882), `RE 108031` (RE250108031). Glued `RECHNUNGRE250110905`.
- Supplier invoice numbers bare/shared (`25001457`, `2025-0217`) collide across suppliers.
- Payer: camt column; SEPA `ABWA+`; n43 `TRANSFERENCIA DE`; MT940 `/ORDP/`. Truncated names `TRANSFERENCIAS RIEGOS VILLAMAR DEL RI`; near-duplicate masters.
- Batch fee reuses batch id: `COMISION REMESA 03250603661 125 RECIBOS` (-18.75 vs 89,776.76 batch).
- Confirming `REMESA CF0100... 3 PROVEEDORES`, debited up to 3 months later, file may be in earlier phase folder.
- Bank line carries e2e `PR010025061275-0083` and run id `PR010025061275`. DD e2e `02250603725-0001`; mandate `VTM-DE-0001153`.
- Advice refs via `EREF+`, `CONCEPTO:`; noisy (`PAY-2025-002301` vs `PAY-2025-002307`); `REM/2025/1598` looks like a doc number. Reasons `-60.11 SHORT DELIVERY`, `PENAL. RETRASO`.
- Card: bank credits net, orders cleared at gross. Card net total once expanded 38 orders onto unrelated transfer.
- Credit mentioning "devolución" is usually retention release/re-payment. Release words: RETENTION RELEASE, LIBERACION RETENCION, RET. GARANTIA, DEVOLUCION GARANTIA, LIBERACION GARANTIA, FREIGABE EINBEHALT.
- GB/CH payers golden SHORT_BANK_CHARGES despite SEPA. Late discount = PARTIAL.
- PDF clause anchors (pdf/clauses.py:22-35): instalments ("instalments at", vencimientos, plazos, raten, échéances); retention ("retención en garantía", "retenue de garantie", einbehalt); advance ("a deducir anticipo", acompte, anzahlung); replaces ("anula y sustituye", "annule et remplace", ersetzt, "cancels and replaces"); iban_change; assignment ("cedido a confirming", "assigned to", "abgetreten an", factoring); credit_note direction offset/refund. `Nº`->"no".
- Clause counts sample: assignment 407, instalments 231, replaces 200, retention 86, credit_note 61, service_month 26, iban_change 18, advance 12. Live: assignment 1497, instalments 1308, replaces 1009, credit_note 698, rappel 193, retention 155, service_month 102, iban_change 57, advance 49.

## Other bugs
- decide cross_currency uses account currency while cash already moved to orig_currency (engine.py:886 vs excepts.py:111).
- _returned fallback takes first token's first item, no narrowing/side check.
- ops by_key first-writer-wins on recurring mandate ids -> returns map to oldest batch.
- Withholding passes when party country unknown.
- Advice near-miss scans every key per token (perf).

## Diagnostics existing
- insights.py: per-line diff vs golden: MISSING_ITEM, EXTRA_ITEM, AMOUNT_OFF, WRONG_EXCEPTION, EXPECTED_ITEM_GOT_CATEGORY, EXPECTED_CATEGORY_GOT_ITEM, WRONG_CATEGORY, LINE_MISSING.
- misses.py: classifies golden-item lines filed as category: no_trace/no_reference/ref_open/ref_settled/ref_partial/ref_absent (needs process --trace).

## Per-segment sample accuracy (score 74.33; seg acc (lines/golden rows))
0.0: ap_duplicate_payment, ap_duplicate_refund, ap_returned_transfer, ar_card_chargeback, ar_cheque_returned, customer_refund(20), nonitem_card_expenses, nonitem_intercompany, nonitem_interest_income, nonitem_internal_transfer.
<0.35: ar_dispute_settlement .019 (53), ar_n_to_m .098 (102/417), ar_short_bank_charges .125, ar_short_withholding .20, ar_overpayment .208 (24), ar_fx .25 (8/566), ar_partial .299 (174), ap_confirming .328 (67/840), ar_advance_deduction .333, nonitem_loan_repayment .333, nonitem_social_security .333, unidentified_lookalike .333 (66), ap_direct_debit .336 (131), ar_cheque_deposit .341 (41/164).
.35-.75: ar_1toN_credit_note .387 (199/2459), unidentified .434 (152), nonitem_bank_reversal .444, ap_factored .455, ar_duplicate_payment .469, nonitem_cash_pool .492 (63), ar_short_dispute .507 (75/8318), ar_retention .527, ar_1toN_text .648 (349/2229), ar_replaced_invoice .667, ar_same_amount_month .681 (185), ar_dd_represented_transfer .683, nonitem_leasing .692, ar_instalment .694 (186), ar_dd_batch .708 (72/5184), ar_ref_noise .716 (2155/5525), ar_third_party_agency .722, ar_new_iban .741, ar_discount_late .744.
>=.75: ar_dd_returned .772, ar_retention_release .773, ap_fx .80, ar_same_amount_fifo .805, ar_short_discount .812, ap_credit_note_netted .814, ar_1toN_remittance .837, ap_supplier_refund .851, customer_advance .875, ar_third_party_parent .889, ar_third_party_confirming .902, nonitem_bank_fees_intl .909, ap_payment_multi .924, ap_partial .944, ar_card_settlement .976, ap_payment_single .980, ar_1to1 .991, nonitem_bank_fees .993; 1.0: ar_1to1_remittance, interest_expense, payroll, tax_payment.
