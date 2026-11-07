# Unwritten rules of the reconciliation data — empirical findings on data/train

All numbers below come from `data/train`, measured with polars scripts in this
directory (`s1.py` … `s12.py`, outputs in `*.out`). `data/train-lite` is
byte-identical to `data/train`: same file sizes, and `cmp` on golden reports no difference. Money was compared in integer cents.
The analysis used the data files only, not the repo docs.

## 1. Inventory and linkage

| file | rows | key columns |
|---|---|---|
| bank_transactions.csv.gz | 1,489,931 lines (2021-07-01 → 2025-06-30), 17 entities, 13 bank×format combos | bank_txn_id, account_id, entity, bank, statement_format, booking_date, value_date, amount (signed, account ccy), currency, bank_code, bank_subcode, ref1, ref2, text, counterparty_name, counterparty_account, end_to_end_id, remittance_info, **orig_amount, orig_currency** |
| golden_allocations.csv.gz | 5,403,411 rows | bank_txn_id, item_id, amount, category, exception, **segment, difficulty** |
| ar_invoices.csv.gz | 3,963,735 (ARI-) | item_id, entity, document_number, issue/due date, customer_id/name, currency, net/tax/total, payment_terms, customer_po, payment_method |
| ap_invoices.csv.gz | 1,530,203 (API-) | item_id, supplier_id/name, supplier_invoice_number, our_po, … |
| credit_notes.csv.gz | 150,215 (ARC- 97,694 / APC- 52,521) | side, document_number, party_id/name, total, related_document, reason |
| bank_statements/ | 2,300 files (n43 768, sta/mt940 863, camt xml 669) | raw statements; the CSV already holds the parsed content |
| remittance_advices/ | 50,891 (csv 18,890 / pdf 16,669 / txt 12,950 / xlsx 2,382) + index.csv (payer_name, received_date) | |
| direct_debit_batches/ | 1,440 csv, 713,554 rows | batch_id, batch_total, end_to_end_id, mandate_id, debtor_name/iban, amount, remittance_info, re_presentation (Y 13,370) |
| payment_runs/ | 1,056 csv, 1,420,037 rows | run_id, run_type (transfers 1.03M / aggregated 326k / confirming 61k), end_to_end_id, beneficiary, transfer_amount, document_number, document_type (INVOICE/CREDIT_NOTE), document_amount |
| card_settlements/ | 240 csv, 147,622 rows | settlement_id, merchant_id, order_number, gross, fee, net |
| cheque_deposits/ | 816 csv, 42,284 rows | deposit_id, deposit_total, cheque_number, drawer, amount, stub_reference (14.8% null) |
| pdfs/ | 131,637 pdf + index (invoice 75,863; supplier_invoice 27,765; credit_note 14,638; supplier_credit_note 10,499; proforma 2,239; bank_account_notice 633) | |

Masters (`data/masters`): bank_accounts, categories, customers (customer_type, iban, sepa_mandate_id, group_id, parent_customer_id), entities, fx_rates (ECB `units_per_eur`, 11 ccys, 1,246 dates), payment_terms, suppliers.

* **Linkage:** every bank line appears in golden (0 missing either way). Every golden item_id exists in an item table (0 orphans). There are 5,260,781 item rows (ARI 3.72M, API 1.43M, ARC 56.9k, APC 48.2k) and 142,630 category rows. No (txn,item) pair is duplicated. **No line mixes item rows and a category row** (0). No line has more than one category row.
* **Each line has exactly one `segment`** (0 lines with more than one), plus a `difficulty` from 1 to 3. The golden file has 72 segments; the full table is in `seg.csv`. The top ones by line share:
  ar_ref_noise 21.0%, ap_payment_multi 11.7%, ar_1to1 9.5%, ap_payment_single 9.35%, ar_same_amount_fifo 3.3%, nonitem_bank_fees 3.15%, ar_1toN_text 3.1%, ar_short_discount 2.9%, ap_direct_debit 2.8%, ar_same_amount_month 2.2%, ap_paid_by_holding 2.2%, ar_instalment 2.2%, ar_partial 1.9%, ar_dd_returned 1.8%, unidentified 1.6%, ar_1toN_credit_note 1.6%, nonitem_bank_fees_intl 1.6%, ap_credit_note_netted 1.2%, ap_partial 1.2%, ap_supplier_refund 1.2%, ar_cheque_deposit 1.0%, ar_n_to_m 0.9%, nonitem_cash_pool 0.8%, ar_dd_batch 0.8%, ap_factored 0.7%, unidentified_lookalike 0.7%, ar_short_withholding 0.7%, ar_card_settlement 0.7%, ar_1toN_remittance 0.6%, ap_fx 0.6%, ar_dispute_settlement 0.5%, ar_dd_represented_transfer 0.5%, ar_short_dispute 0.5%, ar_retention 0.45%, ap_confirming 0.4%, ar_fx 0.4%, ar_discount_late 0.4%, ar_third_party_confirming 0.3%, ar_duplicate_payment 0.3%, ar_retention_release 0.3%, and 32 more below 0.3%.
  The bank_transactions file has **no** segment column, so segment exists only in golden. It is still useful as a training label for a per-line "operation type" classifier.

## 2. Distributions

Items per line:

| items | lines | share |
|---|---|---|
| 0 (category only) | 142,630 | 9.57% |
| 1 | 871,295 | 58.48% |
| 2–5 | 340,299 | 22.84% |
| 6–25 | 111,469 | 7.48% |
| >25 | 24,238 | 1.63% |

* The largest line has 1,540 items. Lines with items average 3.9 items. The >25 bucket is ar_dd_batch 7,469, ar_short_dispute 6,101, ar_1toN_remittance 4,605, ar_card_settlement 1,909, ar_1toN_credit_note 1,603, and so on.
* **Category rows (lines):** BANK_FEES 70,152 (4.71% of all lines), UNIDENTIFIED 34,653 (2.33%), CASH_POOL 12,576, PAYROLL 3,897, INTERCOMPANY 3,187, LEASING 2,832, BANK_REVERSAL 2,664, CUSTOMER_REFUND 2,611, CUSTOMER_ADVANCE 2,008, INTERNAL_TRANSFER 1,936, FX_DEAL 1,551, TAX_PAYMENT 957, LOAN_REPAYMENT 832, SOCIAL_SECURITY 816, CARD_EXPENSES 718, SUPPLIER_REFUND 483, INTEREST_INCOME 320, INSURANCE 220, INTEREST_EXPENSE 181, LOAN_DRAWDOWN 32, TAX_REFUND 4.
* **Category rows can carry an exception.** UNIDENTIFIED category rows always have `exception=UNIDENTIFIED` (34,653 of 34,653). No other category row carries an exception.
* **Exceptions (rows / lines):** FX_DIFFERENCE 113,508 / 15,837; SHORT_DISCOUNT 100,174 / 44,692; PARTIAL 91,664 / 79,491; UNIDENTIFIED 34,653 / 34,653; SHORT_WITHHOLDING 33,818 / 10,210; RETURNED 30,358 / 28,416; SHORT_DISPUTE 20,238 / 8,273; DUPLICATE_PAYMENT 4,927 / 4,927; OVERPAYMENT 2,838 / 2,838; SHORT_BANK_CHARGES 1,130 / 1,130.
* **The exception is per item row, not per line.** On lines that carry an exception, the share of item rows flagged is: DUPLICATE, RETURNED, OVERPAYMENT and SHORT_BANK_CHARGES 100%, SHORT_WITHHOLDING 96%, FX_DIFFERENCE 91%, SHORT_DISCOUNT 89%, PARTIAL 66%, and **SHORT_DISPUTE 0.2%** (on SHORT_DISPUTE lines exactly one row is flagged and the others are normal). DUPLICATE, OVERPAYMENT and SHORT_BANK_CHARGES lines always have exactly one item.
* **Co-occurrence on one line (distinct codes):** FX_DIFFERENCE+PARTIAL 614, FX_DIFFERENCE+SHORT_DISPUTE 563, PARTIAL+SHORT_DISCOUNT 496, PARTIAL+SHORT_DISPUTE 149, SHORT_DISPUTE+SHORT_WITHHOLDING 143, PARTIAL+SHORT_DISCOUNT+SHORT_DISPUTE 82, FX+PARTIAL+DISPUTE 14, DISCOUNT+DISPUTE 6. A single row never carries two codes.

## 3. Exception signatures (golden amount convention + measurable trigger)

**Master rule for the golden amount (same-currency rows):**
* `SHORT_DISCOUNT`, `SHORT_WITHHOLDING`, `SHORT_BANK_CHARGES` and `OVERPAYMENT` → golden amount = **full item total** (100% of rows) even though cash differs. The item is closed (remaining after line = 0 in 100%).
* `PARTIAL` and `SHORT_DISPUTE` → golden = **cash actually applied**, and the item stays open (remaining > 0 in 99.2% / 100%).
* `none` and `DUPLICATE_PAYMENT` → golden = item open amount = cash (1-item lines: golden == |cash| in 99.91% / 100%).
* On 1-item lines with no exception, cash < item total in 5.4% of lines. These are closing payments of a previously part-paid item (ar_partial, ar_instalment, ar_dispute_settlement, ar_retention_release). Golden = the remaining balance, with **no** exception.
* Line arithmetic: when a line holds invoices plus credit notes and no exception, Σ invoices − Σ credit notes == |cash| in **100%** of 47,187 lines. **A credit note's golden amount is positive.**

**Stateful rule (verified with a chronological per-item ledger):** with "remaining after line" = total − cumulative golden (RETURNED counts negative):
* `PARTIAL` ⇔ the item is still open after this line: 99.18% of PARTIAL rows are open after the line, and only 0.06% of `none` rows are.
* `RETURNED` → the item re-opens: remaining = full total in 100%.
* `DUPLICATE_PAYMENT` → remaining becomes negative in 99.45% of rows (the item was already closed).

| code | signature (measured) |
|---|---|
| SHORT_DISCOUNT | cash/total = 0.98 in 39,271 of 39,279 1-item lines and 0.99 in 8. This equals the `early_payment_discount_pct` of the item's terms in **100%** of lines (D2N30US 21,122, D2N30 18,149, D1N60 8). Days from issue to booking: median 5, max 12, with ≤ discount_days in 39,252 of 39,271. **If the same 2% is taken after the window, the row is `PARTIAL`, not SHORT_DISCOUNT** (3,260 lines, segment ar_discount_late; golden = cash, item stays open). Multi-invoice remittances can mix discounted and full rows. |
| SHORT_WITHHOLDING | Only BR01, MA01, MX01 (+42 ES01). Shortfall/total: BR01 1.5% (1,869) or 10% (1,340); MA01 1.5% (980) or 10% (747); MX01 10% (1,785), 1.2–1.3% (1,909). On net: BR 1.5%/10%, MA 1.8%/12%, MX 1.45%/11.6%. 15% of lines are multi-invoice, with every invoice flagged in 96%. Golden = full total. |
| SHORT_BANK_CHARGES | Absolute shortfall on 1-item receipts: p1 = 10.00, median 25.00, p99 71.10 (in account ccy). Common values: 10.00, 14.80, 23.70, 22.20, 15.00, 25.00, 35.00. Concentrated on US01 CAD/USD (529), AU01 (223), ES01/ES02. The text sometimes states it (`/CHGS/EUR20,00` in LORV mt940). Golden = full total. |
| OVERPAYMENT | cash > total. Excess median 13.30 (p10 1.37, p90 175.75) and relative excess median 1%, with p75–p90 exactly 10%. All 2,838 are 1-item lines. Golden = item total (not cash). |
| PARTIAL | Golden/total ratios cluster at 0.5 (16.1k), 0.7 (11.5k), 0.3 (10.6k), 0.98 (9.8k late-discount), 1/3 (9.6k), 0.25 (9.4k), 0.6, 0.4, 0.95 (retention; the 5% is released later as ar_retention_release, 10% of total), 1/6. Instalment plans have 2–6 pieces (3 pieces is the mode, 4,633 items). Advance-deduction lines pay 0.6–0.7. |
| SHORT_DISPUTE | Exactly one row per multi-invoice line. Golden = paid part: ratio 0.70–0.97, median 0.90. The item stays open (100%), and the rest arrives later as `ar_dispute_settlement` (golden = cash, no exception; example 39.03 of 1,301.08). In remittance advices the deduction `reason` is COOP MKT / PRICE DIFF / LOGISTIC FEE / SHORT DELIVERY / PENAL. RETRASO / DAMAGED GOODS (about 1.5k each), versus DISCOUNT 45k and RET 18k. |
| FX_DIFFERENCE | Set on **every cross-currency invoice row that is fully cleared** (ARI 98,745, API 14,763). Cross-currency rows marked PARTIAL (1,007) or SHORT_DISPUTE (1,613) keep that code instead. Cross-currency credit-note rows have no exception (2,766). Golden = item total in item currency (99%). See §5 for rates. |
| DUPLICATE_PAYMENT | The item was already paid once (k=1 in 4,896 of 4,927). Second payment: **same amount 100%**, same account 94.6%, identical text 58%, gap after the first payment median 7 days (p10 2, p90 14, max 50). AR 4,436, AP 491 (an AP duplicate is our own double payment; the refund comes later as SUPPLIER_REFUND category, segment ap_duplicate_refund 483). |
| RETURNED | The cash sign is opposite to the item's normal direction in 100% (ARI receipt reversed 27,224; API payment returned 3,070; APC 64). It follows the original payment by median 7 days (p10 3, p90 13, max 75) on the **same account in 100%**. The original was usually a batch (|return| equals the original line amount in only 10.5%). Golden = item total. Afterwards, 78.8% of returned items are paid again: by the next DD batch (13,235, marked re_presentation=Y in batch files) or by a transfer (ar_dd_represented_transfer 7,488, text "PAGO RECIBO <suffix>"), median 17 days later. Text forms: `RUECKLASTSCHRIFT AC04 MREF+VTM-US-0020228`, `DEVOL.RECIBOS S/REMESA FECHA | MANDATO VTM-ES-…`, `SDD RETURN AM04 MANDATE VTM-IT-…`, `DEVOLUCION TRANSFERENCIA AC04`, `RETURN MD07 … PR0100…`, `RETURNED ITEM CHK 6504375 NSF`, `CHEQUE IMPAYE … STOP PAYMENT`, `CHARGEBACK ****7284 MID 620744903`. A return is linked through the **mandate id** (customers.sepa_mandate_id), the cheque number, or card+MID. |
| UNIDENTIFIED | A category row (no item) with exception=UNIDENTIFIED. All are receipts. `unidentified` (24,299) has 30.6% whole-unit amounts (vs 1.5% for real AR receipts), cash deposits ("INGRESO EFECTIVO/CAJERO") and vague refs. `unidentified_lookalike` (10,354) mimics real customer receipts (`TRANSFERENCIA SEPA | …-LUIS PONS LLORENTE | FACTURA`). 49% of named UNIDENTIFIED counterparties are real customers, so a known payer alone is not evidence of an item. |

Literal examples (format: bank line | item | golden):


**SHORT_DISPUTE**
```
BT-2021-0160081 2021-12-22 US01-HPNB-USD-3 cash=6471.20USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/VENDOR PMT 122221 PAY-2021-001221' cpty='Lakeview Ridge Spa Service Inc.' rinfo='VENDOR PMT 122221 PAY-2021-001221' | item=ARC-2021-7227507 doc=CM092100411 party='Lakeview Ridge Spa Service Inc.' due=None tot=9297.53CAD golden=9297.53 ex=none seg=ar_fx rem_after=0.00
BT-2021-0160081 2021-12-22 US01-HPNB-USD-3 cash=6471.20USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/VENDOR PMT 122221 PAY-2021-001221' cpty='Lakeview Ridge Spa Service Inc.' rinfo='VENDOR PMT 122221 PAY-2021-001221' | item=ARI-2021-1906173 doc=1092115150 party='Lakeview Ridge Spa Service Inc.' due=2021-12-10 tot=1685.90CAD golden=1685.90 ex=FX_DIFFERENCE seg=ar_fx rem_after=0.00
BT-2021-0160081 2021-12-22 US01-HPNB-USD-3 cash=6471.20USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/VENDOR PMT 122221 PAY-2021-001221' cpty='Lakeview Ridge Spa Service Inc.' rinfo='VENDOR PMT 122221 PAY-2021-001221' | item=ARI-2021-6541200 doc=1092115011 party='Lakeview Ridge Spa Service Inc.' due=2021-12-05 tot=17767.50CAD golden=15990.75 ex=SHORT_DISPUTE seg=ar_fx rem_after=1776.75
BT-2024-0200972 2024-07-10 US01-HPNB-USD-3 cash=12372.75USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/PMT ADVICE REM/2024/1494' cpty='Lakeview Ridge Spa Service Inc.' rinfo='PMT ADVICE REM/2024/1494' | item=ARI-2024-0230803 doc=1092414664 party='Lakeview Ridge Spa Service Inc.' due=2024-07-04 tot=7630.39CAD golden=7248.88 ex=SHORT_DISPUTE seg=ar_fx rem_after=381.51
BT-2024-0200972 2024-07-10 US01-HPNB-USD-3 cash=12372.75USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/PMT ADVICE REM/2024/1494' cpty='Lakeview Ridge Spa Service Inc.' rinfo='PMT ADVICE REM/2024/1494' | item=ARI-2024-0490701 doc=1092414078 party='Lakeview Ridge Spa Service Inc.' due=2024-06-30 tot=3420.60CAD golden=3420.60 ex=FX_DIFFERENCE seg=ar_fx rem_after=0.00
BT-2024-0200972 2024-07-10 US01-HPNB-USD-3 cash=12372.75USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/PMT ADVICE REM/2024/1494' cpty='Lakeview Ridge Spa Service Inc.' rinfo='PMT ADVICE REM/2024/1494' | item=ARI-2024-1325378 doc=1092413889 party='Lakeview Ridge Spa Service Inc.' due=2024-06-28 tot=5557.39CAD golden=5557.39 ex=FX_DIFFERENCE seg=ar_fx rem_after=0.00
BT-2024-0200972 2024-07-10 US01-HPNB-USD-3 cash=12372.75USD text='WIRE IN /ORG/LAKEVIEW RIDGE SPA SERVICE INC. /RFB/NOTPROVIDED /OBI/PMT ADVICE REM/2024/1494' cpty='Lakeview Ridge Spa Service Inc.' rinfo='PMT ADVICE REM/2024/1494' | item=ARI-2024-7839909 doc=1092414822 party='Lakeview Ridge Spa Service Inc.' due=2024-07-05 tot=662.91CAD golden=649.65 ex=PARTIAL seg=ar_fx rem_after=13.26
BT-2024-0292877 2024-10-02 US01-HPNB-USD cash=1738.75USD text='ACH CREDIT SPA SERVICE PACI CCD PAYMENT ID:4627998839' cpty='Spa Service Pacific Corp.' rinfo='REMITTANCE 9926100255' | item=ARI-2024-1901383 doc=1102416261 party='Spa Service Pacific Corp.' due=2024-09-26 tot=1792.52USD golden=1738.75 ex=SHORT_DISPUTE seg=ar_short_dispute rem_after=53.77
```

**PARTIAL**
```
BT-2021-0154890 2021-12-14 ES01-PENA-EUR cash=770.18EUR text='TRANSFER INMEDIATA | 83602013-RUBIO SPA S.A. | FRA. FV21-0215045' cpty='None' rinfo='None' | item=ARI-2021-7165763 doc=FV21-0215045 party='Rubio Spa S.A.' due=2021-12-12 tot=1540.37EUR golden=770.18 ex=PARTIAL seg=ar_instalment rem_after=770.19
BT-2024-0212031 2024-07-22 AU01-WATT-AUD-2 cash=7242.02AUD text='DIRECT CREDIT LEISURE PORT ELLERY  PAYMENT INV INV24-' cpty='Leisure Port Ellery Pty Ltd' rinfo='PAYMENT INV INV24-0311848' | item=ARI-2024-8044281 doc=INV24-0311848 party='Leisure Port Ellery Pty Ltd' due=2024-06-21 tot=21726.06AUD golden=7242.02 ex=PARTIAL seg=ar_instalment rem_after=7242.02
BT-2024-0306085 2024-10-14 US01-HPNB-USD cash=792.95USD text='ACH CREDIT BROOKS AQUATICS  CCD PAYMENT ID:4460355546 RMR*IV*1092419307*PI*792.95\' cpty='Brooks Aquatics Co.' rinfo='PAYMENT INV 1092419307' | item=ARI-2024-0731304 doc=1092419307 party='Brooks Aquatics Co.' due=2024-09-27 tot=2378.86USD golden=792.95 ex=PARTIAL seg=ar_instalment rem_after=1585.91
```

**SHORT_DISCOUNT**
```
BT-2021-0164404 2021-12-27 CH01-RHEI-CHF cash=1106.06CHF text='SEPA-UEBERWEISUNG EREF+20211227229490 SVWZ+RE-NR RE21010' cpty='Fernando Arnau Ribas' rinfo='RE-NR RE21010' | item=ARI-2021-3829295 doc=RE210105729 party='Fernando Arnau Ribas' due=2022-01-19 tot=1128.63CHF golden=1128.63 ex=SHORT_DISCOUNT seg=ar_short_discount rem_after=0.00
BT-2024-0222437 2024-07-25 US01-HPNB-USD-3 cash=444.18USD text='ACH CREDIT POOL & SPA FAIRH CCD PAYMENT ID:2882016155' cpty='Pool & Spa Fairhaven Inc.' rinfo='SUPPLIES' | item=ARI-2024-8467845 doc=1122413639 party='Pool & Spa Fairhaven Inc.' due=2024-08-22 tot=453.25USD golden=453.25 ex=SHORT_DISCOUNT seg=ar_short_discount rem_after=0.00
BT-2024-0325553 2024-11-04 AU01-WATT-AUD-2 cash=13801.34AUD text='DIRECT CREDIT PORT ELLERY POOL BUI ' cpty='Port Ellery Pool Builders Pty Ltd' rinfo='None' | item=ARI-2024-0715475 doc=INV24-0220681 party='Port Ellery Pool Builders Pty Ltd' due=2024-11-27 tot=14083.00AUD golden=14083.00 ex=SHORT_DISCOUNT seg=ar_short_discount rem_after=0.00
```

**SHORT_WITHHOLDING**
```
BT-2021-0156192 2021-12-16 BR01-CAZU-BRL cash=4688.73BRL text='PIX RECEBIDO PORTO VERDE BEM-ESTAR S.A. 211216640199 PORTO' cpty='None' rinfo='None' | item=ARI-2021-4349655 doc=NF0121012131 party='Porto Verde Bem-Estar S.A.' due=2021-12-05 tot=1883.10BRL golden=1883.10 ex=SHORT_WITHHOLDING seg=ar_short_withholding rem_after=0.00
BT-2021-0156192 2021-12-16 BR01-CAZU-BRL cash=4688.73BRL text='PIX RECEBIDO PORTO VERDE BEM-ESTAR S.A. 211216640199 PORTO' cpty='None' rinfo='None' | item=ARI-2021-7264892 doc=NF0121012022 party='Porto Verde Bem-Estar S.A.' due=2021-12-04 tot=2877.04BRL golden=2877.04 ex=SHORT_WITHHOLDING seg=ar_short_withholding rem_after=0.00
BT-2024-0212067 2024-07-22 BR01-CAZU-BRL cash=2531.02BRL text='LIQUIDACAO BOLETO 2529128932-3 RIVAS VILA REGA S.A. UEBERWEISUNG' cpty='None' rinfo='None' | item=ARI-2024-6433262 doc=NF0124014678 party='Rivas Vila Rega S.A.' due=2024-07-18 tot=2569.56BRL golden=2569.56 ex=SHORT_WITHHOLDING seg=ar_short_withholding rem_after=0.00
BT-2024-0309346 2024-10-18 MX01-MEZQ-MXN cash=6658.15MXN text='SPEI RECIBIDO CLAVE 006493098321315827 REF 3195109 CONCEPTO PAGO OCTUBRE HIDROTECNIA BLUE S.A. DE C.V.' cpty='None' rinfo='None' | item=ARI-2023-1153068 doc=A01-2301426 party='Hidrotecnia Blue S.A. de C.V.' due=2023-04-09 tot=7397.95MXN golden=7397.95 ex=SHORT_WITHHOLDING seg=ar_short_withholding rem_after=0.00
```

**OVERPAYMENT**
```
BT-2021-0168871 2021-12-30 US01-HPNB-USD-3 cash=334.84USD text='ACH CREDIT LAKE HARMON OUTD CCD PAYMENT ID:7577755007 RMR*IV*116057*PI*334.84\' cpty='Lake Harmon Outdoor Living Corp.' rinfo='PAYMENT INV 116057' | item=ARI-2021-0499619 doc=1082116057 party='Lake Harmon Outdoor Living Corp.' due=2021-11-24 tot=333.84USD golden=333.84 ex=OVERPAYMENT seg=ar_overpayment rem_after=0.00
BT-2024-0223305 2024-07-29 AU01-WATT-AUD cash=868.06AUD text='DIRECT CREDIT BACKYARD PRIME PTY L INV INV24-0312109' cpty='Backyard Prime Pty Ltd' rinfo='INV INV24-0312109' | item=ARI-2024-5842178 doc=INV24-0312119 party='Backyard Prime Pty Ltd' due=2024-07-26 tot=866.06AUD golden=866.06 ex=OVERPAYMENT seg=ar_overpayment rem_after=0.00
BT-2024-0318099 2024-10-25 ES01-MONC-EUR cash=465.89EUR text='TRANSFERENCIAS SANTIAGO SOLER NIETO | SANTIAGO S FACTURA FV24-0112881' cpty='None' rinfo='None' | item=ARI-2024-6204727 doc=FV24-0112881 party='Santiago Soler Nieto' due=2024-08-04 tot=460.89EUR golden=460.89 ex=OVERPAYMENT seg=ar_overpayment rem_after=0.00
```

**SHORT_BANK_CHARGES**
```
BT-2021-0143492 2021-12-02 US01-HPNB-CAD cash=25375.77CAD text='WIRE IN /ORG/MYERS & NELSON HYDRO INC. /RFB/NOTPROVIDED /OBI/MYERS' cpty='Myers & Nelson Hydro Inc.' rinfo='MYERS' | item=ARI-2021-8171038 doc=1052110954 party='Myers & Nelson Hydro Inc.' due=2021-12-02 tot=25427.57CAD golden=25427.57 ex=SHORT_BANK_CHARGES seg=ar_short_bank_charges rem_after=0.00
BT-2024-0244397 2024-08-19 FR01-LORV-EUR-2 cash=1683.95EUR text='/ORDP/RFE EAUX SARL/REMI/PAYMENT/EREF/NOTPROVIDED /CHGS/EUR20,00' cpty='None' rinfo='None' | item=ARI-2024-7552646 doc=FA240116068 party='RFE Eaux SARL' due=2024-08-11 tot=1703.95EUR golden=1703.95 ex=SHORT_BANK_CHARGES seg=ar_short_bank_charges rem_after=0.00
BT-2024-0339902 2024-11-18 AU01-WATT-AUD-2 cash=17912.65AUD text='DIRECT CREDIT SPA SERVICE THOMAS L PAYMENT' cpty='Spa Service Thomas Ltd' rinfo='PAYMENT' | item=ARI-2024-1255104 doc=INV24-0318537 party='Spa Service Thomas Ltd' due=2024-10-20 tot=17928.45AUD golden=17928.45 ex=SHORT_BANK_CHARGES seg=ar_short_bank_charges rem_after=0.00
```

**FX_DIFFERENCE**
```
BT-2021-0149129 2021-12-09 FR01-LORV-EUR-2 cash=-7981.79EUR text='VIR SEPA EMIS /A MOTORS ROSS LTD /MOTIF FRAS 2905 R2910 /REF PR010021120928-0174 /OCMT/USD8975,25/EXCH/0,889311' cpty='None' rinfo='None' | item=APC-2021-8852767 doc=R2910 party='Motors Ross Ltd' due=None tot=4147.20USD golden=4147.20 ex=none seg=ap_fx rem_after=0.00
BT-2021-0149129 2021-12-09 FR01-LORV-EUR-2 cash=-7981.79EUR text='VIR SEPA EMIS /A MOTORS ROSS LTD /MOTIF FRAS 2905 R2910 /REF PR010021120928-0174 /OCMT/USD8975,25/EXCH/0,889311' cpty='None' rinfo='None' | item=API-2021-3669524 doc=2905 party='Motors Ross Ltd' due=2021-11-13 tot=13122.45USD golden=13122.45 ex=FX_DIFFERENCE seg=ap_fx rem_after=0.00
BT-2024-0205813 2024-07-11 US01-HPNB-USD-2 cash=-7727.41USD text='ACH DEBIT MARTÍN CHEMICALS VENDOR PMT' cpty='Martín Chemicals GmbH' rinfo='FRA A000356' | item=API-2024-0231827 doc=A000356 party='Martín Chemicals GmbH' due=2024-07-15 tot=7086.16EUR golden=7086.16 ex=FX_DIFFERENCE seg=ap_fx rem_after=0.00
BT-2024-0301159 2024-10-10 FR01-LORV-EUR cash=-13125.69EUR text='VIR SEPA EMIS /A TE9 ENERGY CO., LTD. /MOTIF FRAS F24/03390 F24/03391 /REF PR010024101072-0422 /OCMT/USD14294,68/EXCH/0,918222' cpty='None' rinfo='None' | item=API-2024-1435023 doc=F24/03391 party='TE9 Energy Co., Ltd.' due=2024-10-15 tot=2964.28USD golden=2964.28 ex=FX_DIFFERENCE seg=ap_fx rem_after=0.00
BT-2024-0301159 2024-10-10 FR01-LORV-EUR cash=-13125.69EUR text='VIR SEPA EMIS /A TE9 ENERGY CO., LTD. /MOTIF FRAS F24/03390 F24/03391 /REF PR010024101072-0422 /OCMT/USD14294,68/EXCH/0,918222' cpty='None' rinfo='None' | item=API-2024-7282309 doc=F24/03390 party='TE9 Energy Co., Ltd.' due=2024-10-13 tot=11330.40USD golden=11330.40 ex=FX_DIFFERENCE seg=ap_fx rem_after=0.00
```

**DUPLICATE_PAYMENT**
```
BT-2021-0144163 2021-12-03 FR01-LORV-EUR-2 cash=1025.52EUR text='VIR SEPA RECU /DE RSA AQUATIQUE EURL /MOTIF FACT 121249 /REF 20211203553736' cpty='None' rinfo='None' | item=ARI-2021-9129834 doc=FA210121249 party='RSA Aquatique EURL' due=2021-11-26 tot=1025.52EUR golden=1025.52 ex=DUPLICATE_PAYMENT seg=ar_duplicate_payment rem_after=-1025.52
BT-2024-0210202 2024-07-17 US01-HPNB-USD cash=613.09USD text='ACH CREDIT NOAH YOUNG CCD PAYMENT ID:6509928703' cpty='Noah Young' rinfo='ACCOUNT PAYMENT' | item=ARI-2024-8606500 doc=1102408992 party='Noah Young' due=2024-07-06 tot=613.09USD golden=613.09 ex=DUPLICATE_PAYMENT seg=ar_duplicate_payment rem_after=-613.09
BT-2024-0310858 2024-10-21 US01-HPNB-USD cash=589.15USD text='ACH CREDIT ROSS & HILL LEIS CCD PAYMENT ID:5985607439' cpty='Ross & Hill Leisure Co.' rinfo='INVOICE 1122416561' | item=ARI-2024-0960299 doc=1122416561 party='Ross & Hill Leisure Co.' due=2024-10-12 tot=589.15USD golden=589.15 ex=DUPLICATE_PAYMENT seg=ar_duplicate_payment rem_after=-589.15
```

**RETURNED**
```
BT-2021-0147277 2021-12-08 US01-HPNB-USD cash=-651.75USD text='RUECKLASTSCHRIFT AC06 MREF+VTM-US-0014562' cpty='Sun Prairie Hills Landscapes Corp.' rinfo='None' | item=ARI-2021-1898305 doc=1112112165 party='Sun Prairie Hills Landscapes Corp.' due=2021-12-01 tot=651.75USD golden=651.75 ex=RETURNED seg=ar_dd_returned rem_after=651.75
BT-2024-0200706 2024-07-10 US01-HPNB-USD cash=-1635.30USD text='RUECKLASTSCHRIFT MD01 MREF+VTM-US-0010070' cpty='Hydro Sanders Pro LLC' rinfo='None' | item=ARI-2024-8641324 doc=1022415391 party='Hydro Sanders Pro LLC' due=2024-07-03 tot=1635.30USD golden=1635.30 ex=RETURNED seg=ar_dd_returned rem_after=1635.30
BT-2024-0302825 2024-10-10 US01-HPNB-USD cash=-1795.28USD text='RUECKLASTSCHRIFT AM04 MREF+VTM-US-0016408' cpty='Jessica Mitchell' rinfo='None' | item=ARI-2024-1874851 doc=1052413036 party='Jessica Mitchell' due=2024-10-02 tot=1795.28USD golden=1795.28 ex=RETURNED seg=ar_dd_returned rem_after=1795.28
```
## 4. Bank text: references, names, batch ids

Measured on 862,418 one-item lines (item ccy = account ccy). "Any field" means text + remittance_info + ref1 + ref2 + end_to_end_id, normalised to uppercase A–Z0–9.

| metric | all | ARI | API |
|---|---|---|---|
| full document number in `text` | 29.7% | 26.4% | 39.9% |
| full document number in any field | 43.1% | 33.5% | 73.2% |
| only the numeric tail (prefix dropped) found | 1.9% | 2.0% | 0.1% |
| only the first 6 chars found (truncated) | 5.0% | 6.7% | 0.0% |
| any reference evidence at all | 50.0% | 42.2% | 73.3% |
| counterparty_name == party name (exact, case-insensitive) | 49.0% | 51.2% | 42.0% |
| first ≥4-char token of the party name appears in the text | 80.4% | 82.4% | 73.7% |

* **By bank/format** (full doc in any field / counterparty_name present / name token in text):
  HPNB camt 42.6% / 98.7% / 86.6%; RHEI camt 45.8% / 99.9% / 41.9%; WATT camt 50.9% / 100% / 89.7%; PENA n43 23.8% / 0% / 53.7%; MONC n43 36.2% / 0% / 76.0%; RIBA n43 34.1% / 0% / 70.7%; LORV mt940 55.2% / 0% / 96.1%; LING 56.7% / 0 / 97.5%; VALD 48.2% / 0 / 97.1%; CAZU 43.6% / 0 / 97.7%; MEZQ 44.0% / 0 / 96.5%; RHEI mt940 44.0% / 0 / 96.4%; NORT 37.8% / 0 / 94.8%.
  **Only camt053 has counterparty_name and remittance_info.** For camt, the reference sits in `remittance_info` far more often than in `text`: HPNB 16% in text vs 43% in any field, WATT 13% vs 51%. When counterparty_name is present it equals the master name in about 96% of lines. n43 truncates names and wraps text at fixed width with ` | ` separators. This splits tokens: `FRA FV | 21-0319668`, `TRANSPORTES MONTERO LD | A.`, `FV22-03190 | 13`. **Join the ` | ` fragments before searching.** mt940 (NORT) cuts text at about 40 chars (`FP HOWARD PLASTICS LL FRAS A002005 A0020`). camt `text` is truncated (`INVOICE INV22-0118`) while `remittance_info` carries the full ref.
* **Reference mutations seen** (examples per bank in the appendix):
  - prefix dropped: `RE 205624` → RE240205624, `RECIBO 0102689`, `PAGO RECIBO 307041` → 1052307041, `FACT 121249` → FA210121249, `RMR*IV*116057` → 1082116057;
  - truncated: `INV21-0303` → INV21-0303683, `RE RE2102058` → RE210205873;
  - free text without a ref: `PAYMENT`, `SUPPLIES`, `SERVICE FEBRUARY 2022`, `N/REF 2023-9220`, `S/PED PED-39150` (a PO number);
  - a different own reference: `PAY-2021-…` in ref1 or e2e, `PAYMENT ID:`, `CLAVE`, `REF 2203242`.
* **By segment** (full doc in any field): ar_1to1 98.7%, ap_payment_single 89.6%, ar_dispute_settlement 96.9%, ar_third_party_parent 93.4%; but **ar_ref_noise 0.0%** (17% partial evidence), ar_same_amount_fifo 0%, ar_same_amount_month 0%, ar_dd_returned 0%, ar_cheque_deposit 0%, ap_confirming 0%. ar_replaced_invoice shows the **old (replaced) doc number** in 97.1%; golden goes to the replacement invoice (doc number differs in the last digits, same party, same amount).
* **Amount uniqueness** among open items at booking time (764k full-payment one-item lines, open = issued ≤ booking and first payment ≥ booking):
  - unique in (entity, currency, amount): 67.4% overall (ARI 60.8%, API 85.6%);
  - unique within the paying party: 75.7% (ARI 68.2%, API 96.5%);
  - for ar_ref_noise it is 88.7% within the entity and **99.97% within the party**, so name + amount solves ref noise;
  - for ar_same_amount_* it is only 7.7% and 13.6%, so FIFO decides (§6).
  - **ap_paid_by_holding is unique in only 0.4% within its own entity but 97.5% within the party**: ES00 pays ES01/ES02/PT01 supplier invoices (§8).
* **Batch ids:**
  - **DD**: batch_id is in the text 100% (`ABONO REMESA SEPA CORE 05210716586 | PRESENTADA 210716 215 RECIBOS`, `ACH DEBIT ORIGINATION BATCH 12210723711 24 ENTRIES`). |cash| == batch_total 100%. Golden = all batch rows. Matching rows by `remittance_info == doc` works for 90%; the other 10% are `RECIBO <suffix>` and match by debtor_name + amount 100%.
  - **Cheques**: deposit_id is in the text 100% (`REMISE DE CHEQUES RCH22020302222 4 CHQ`, `LOCKBOX LBX… DEPOSIT 4 ITEMS`). |cash| == deposit_total 100%. stub_reference == doc for only 43% (15% null), so the rest needs drawer + amount.
  - **Cards**: the text carries the **merchant id + date** (`CARD SETTLEMENT MID 040654992 2021-06-29 4 TRX`, `REMISE CB 579821918 DU 090921 6 OP`), not the settlement_id (10.5%). |cash| == Σnet 100%, golden docs == order_numbers 100%. The card fee is netted (gross − fee), and golden = gross per order.
  - **Confirming runs**: run_id in text 99%. Golden ⊆ run docs 100%, including credit notes in the run (document_type CREDIT_NOTE, negative amount).
  - **AP transfers**: e2e `PR0100YYMMDDNN-NNNN` = run_id + seq for camt lines.
* **Remittance advices (CSV, 18,889 files):** the payment_ref appears in the bank text for only a minority. Linking by **amount = Σpaid, same currency, booking_date = payment_date** finds a match for 96.5% of advices, unique for 96.4% (0-day gap for 18,226). On linked lines, golden docs == advice docs in **99.97%**. Segments: ar_ref_noise 5,526, ar_1toN_remittance 3,910, ar_short_dispute 3,010, ar_1toN_text 2,889, ar_1toN_credit_note 1,125.

## 5. Allocation amount and FX

* Golden amount is always in the **item currency** and positive. Same currency: see §3 (open amount for full/short-closing, cash for partial/dispute).
* Cross-currency: 118,933 rows on 16,465 lines (ar_fx 101,731, ap_fx 15,423, plus a few in other AR segments). Golden = item total in 99% of rows.
* **`orig_amount`/`orig_currency` on the bank line is the item-currency amount**: orig_currency == item currency in 100% and is present on 97.9% of clean FX lines. orig_amount == Σ(invoices − credit notes) in 95.1%. LORV/LING-style text also carries `/OCMT/USD8975,25/EXCH/0,889311` (41% of FX lines).
* Implied rate (cash / item amount) vs ECB cross rate (units_per_eur, forward-filled). Share of 14,958 clean lines within 3% for each rate date: **booking date 96.3%**, value date 96.0%, due date 89.1%, issue date 67.9%. The deviation is a one-sided bank spread:
  - payments cost +0.42% median (p10 0.15%, p90 1.3%);
  - receipts get −0.45% median (p90 −0.11%, p10 −2.1%);
  - an exact rate (within 5 bp) is rare (1.8%), so the generator applies a spread to the booking-date rate.
  - Top pairs: USD→EUR 4,526, CAD→USD 3,243, USD→MXN 1,571, EUR→USD 1,435.

## 6. Ordering among same-party, same-amount open invoices

On full single-invoice receipts with more than one open candidate of the same party, currency and amount (open = not yet paid by an earlier bank_txn_id), I excluded never-paid items issued before the bank window. That removes the 2021-03…06 opening items, which are never settled in TRAIN (220k never-paid items issued in 2021):
* ar_same_amount_fifo: **oldest due date = golden in 97.9%** (newest 0.4%), 39.9k lines.
* ar_1to1 lines with ties: oldest 97.6%.
* With the stale pre-window items left in, the same figures drop to 73.5%. **Excluding stale opening items matters.**
* Due date, issue date and doc number give the same order here: candidates had distinct due dates in 100% of cases.
* ar_same_amount_month (`SERVICE FEBRUARY 2022`, `SERVICIOS JULIO 2021`, `CUOTA MANTENIMIENTO FEBRERO`): FIFO gives only 71%. The text names a month in 48% of these lines, and when it does, **invoice issue month = text month + 1 in 100%** (13,571 of 13,571).
* ar_dd_represented_transfer: golden is the **returned** item (FIFO 7%). Look up the item reopened by a return.

## 7. Timing

* Booking − due for the first payment of each item:
  - **AR** median +8 days (p10 0, p90 +46, p1 −38);
  - **AP** median +2 (p10 −4, p90 +24).
* By AR terms: DD15/DD30 0–3 days (collection on due date + 0–3), CASH +1…+14, N30US +9, T30 +18, T60 +15, T90 +13. D2N30/D1N60 are bimodal: −23…−52 for early discounters, otherwise +8.
* By customer_type: INSTALLER_DD 0–3; WEB_CARD +1…+4 (settlement); KEY_ACCOUNT +13; DISTRIBUTOR +19; INSTALLER +7.
* By segment: ar_short_discount −24 (≈ issue + 5); ap_direct_debit 0–2; ar_card_settlement 1–4; ar_n_to_m long tail (p99 630 days).
* **No payment ever precedes the invoice issue date** (0 of 5.26M rows), so candidate items can be filtered to issue_date ≤ booking_date.
* due = issue + term days in 100% for every term except T60D25, which pays on the 25th (median 75 days).
* value_date − booking_date: 0 days for 83%, 2 for 8%, 4 for 4%.

## 8. Surprises and structural facts

* **Cross-entity (holding pays for subsidiaries):** 2.05% of item rows belong to a different entity than the bank account. **All of them are ES00 paying ES01, ES02 or PT01 AP items**: ap_paid_by_holding 82.6k rows, plus ap_credit_note_netted, ap_partial, ap_direct_debit, ap_factored and ap_fx. ES00 AP lines therefore need candidates from all Iberian entities.
* **No AR+AP netting**: a line never mixes AR and AP items (0 lines). The mixes are ARI 841k, API 430k, ARC+ARI 35.5k, APC+API 22.3k, APC-only 17.8k (supplier refunds, receipts), ARC-only 325 (we pay out a customer credit note / rebate: `REBATE Q3 2021 CM132100330`).
* **Credit notes** take part only when netted (golden positive; Σinv − Σcn = cash in 100%) or alone as refunds. 40,764 ARC and 4,400 APC are never touched in TRAIN.
* **Factoring and confirming:** the payee in the text is a factor (`CREDIFÁCIL COMERCIAL EFC`, `NORTE PAGOS CONFIRMING EFC`) and not the supplier, so supplier-name matching fails (ap_factored name token 0%). Use the doc number (84.5% present). On the AR side, `FATIR… | ORD. <customer> FRA …` / `ABONO CONFIRMING` comes from a bank paying on the customer's behalf.
* **Third parties:** collection agencies (`GESTIÓN DE IMPAGADOS DELTACOBRO … EXPTE 55225 ROBERT WILSON 0113829`, cpty `ARVENTO RECOVERIES LTD`) pay on behalf of the customer. The parent company pays a child's invoice (ar_third_party_parent; customers.parent_customer_id).
* **Fees are never embedded as item rows.** Card fees are netted inside the settlement. Batch fees and transfer fees are separate BANK_FEES lines (`COMISION REMESA …`, `SERV. EM. TRANSF.`).
* **Never-paid items:** 349k ARI, 127k API, 41k ARC and 4.4k APC never appear in golden. 220k were issued in 2021 (before or at the window start) and 247k in 2025 (still open at the end).
* Items per line reach 1,540 (DD batches). An item is paid by up to 8 lines (instalments, dispute and retention follow-ups, return + re-presentation).
* Golden `amount` is never negative. The sign lives only on the bank line.

## 9. Category-only lines

* Sign is deterministic per category. BANK_FEES, PAYROLL, LEASING, TAX, SOCIAL_SECURITY, CARD_EXPENSES, INSURANCE, LOAN_REPAYMENT, CUSTOMER_REFUND and INTEREST_EXPENSE are always negative. UNIDENTIFIED, CUSTOMER_ADVANCE, SUPPLIER_REFUND, INTEREST_INCOME, LOAN_DRAWDOWN and TAX_REFUND are always positive. CASH_POOL, INTERCOMPANY, INTERNAL_TRANSFER and BANK_REVERSAL are both. **INTEREST_INCOME vs INTEREST_EXPENSE share the text `LIQUIDACION INTERESES nTyy`; only the sign separates them.**
* The first two text tokens (digits masked) map to the line class (item vs each category) with **97.7% majority-class accuracy** over all 1.49M lines (11,656 keys). For templated categories the purity is 100%: `SERV TRANSF`, `COMISION TRANSFERENCIA`, `FRAIS VIREMENT`, `COM TRANSF` → BANK_FEES; `CASH CONCENTRATION`, `ZBA SWEEP`, `TRASPASO CASH` → CASH_POOL; `NOMINA`, `PAYROLL ACH` → PAYROLL; `TRASPASO CTA` → INTERNAL_TRANSFER; `TRANSF/FINANCIACION/PRESTAMO … VALTERMIA` → INTERCOMPANY; `CUOTA LEASING/ARRENDAMIENTO/RENTING/LSG` → LEASING; `FX DEAL`, `COMPRA/VENTA DIVISA` → FX_DEAL; `ANULACION OPERACION INDEBIDA` → BANK_REVERSAL; `ADEUDO MENSUAL DE TARJETA` → CARD_EXPENSES; `DISPOSICION POLIZA` → LOAN_DRAWDOWN; `CARGO POR AMORTIZACION DE PRESTAMO` → LOAN_REPAYMENT; `DEVOLUCION AEAT` → TAX_REFUND; `SOCIAL CHARGES`/`TGSS` → SOCIAL_SECURITY; `VAT RETURN`/`TAX PAYMENT`/`IMPUESTOS MOD` → TAX_PAYMENT.
* bank_code alone gives 96.2% majority accuracy. For example, NCHG / 17 / ACMT/MCOP/CHRG → BANK_FEES; CAMT/ACCB/ZABA → CASH_POOL; NSAL / PMNT/ICDT/SALA → PAYROLL.
* Overall, 76.4% of category lines fall in a key whose majority is their category. The rest look like normal transfers:
  - **CUSTOMER_ADVANCE**: the rule "positive and the text/remittance contains `PROFORMA` or `PF\d`" gives precision 99.5% and recall 100%.
  - **CUSTOMER_REFUND**: "negative and `REFUND|DEVOLUCION PAGO|DUPLICAD|…`" gives precision 100% and recall 96.2%.
  - **SUPPLIER_REFUND**: the keywords (`REEMBOLSO`, `PAGO DOBLE`, `DEVOLUCION SALDO A FAVOR`) are shared with ap_supplier_refund item lines (an APC item), so precision is only 6.3%. The rule: if an open supplier credit note of that amount exists, it is an item; if the supplier was double-paid earlier (ap_duplicate_payment), it is the category.
  - **UNIDENTIFIED** is the residual class; see §3.

Category examples:

- **BANK_FEES**: `SERV. EM. TRANSF.` (-136.05 MXN, MEZQ/NCHG) ; `SERV. EM. TRANSF.` (-31.43 EUR, PENA/17) ; `SERV. EM. TRANSF.` (-63.70 USD, HPNB/ACMT/MCOP/CHRG)
- **BANK_REVERSAL**: `ANULACION OPERACION INDEBIDA N 2021090726891406 RECIBO` (1442.17 MAD, LORV/NMSC) ; `ANULACION OPERACION INDEBIDA N 2024030 | 732606410 COMISION` (6094.78 EUR, MONC/98) ; `RECIBO` (186.40 EUR, VALD/NMSC)
- **CARD_EXPENSES**: `ADEUDO MENSUAL DE TARJETA ************7008` (-6526.13 EUR, RHEI/PMNT/CCRD/OTHR) ; `ADEUDO MENSUAL DE TARJETA ************ | 0310` (-1285.68 EUR, PENA/12) ; `ADEUDO MENSUAL DE TARJETA ************9557` (-1297.05 EUR, LORV/NMSC)
- **CASH_POOL**: `TRASPASO CASH POOLING NIVELACION SALDO` (-18374.15 EUR, PENA/99) ; `CASH CONCENTRATION PT01 5222` (3645.29 EUR, PENA/99) ; `TRASPASO CASH POOLING NIVELACION SALDO` (87153.40 EUR, PENA/99)
- **CUSTOMER_ADVANCE**: `SEPA-UEBERWEISUNG EREF+NOTPROVIDED SVWZ+PROFORMA PF21-0200009` (1701.45 EUR, RHEI/PMNT/RCDT/ESCT) ; `ACH CREDIT MARTIN POOL SUPP CCD PAYMENT ID:1254683001 RMR*IV*PF24-1300003*PI*415.40\` (415.40 USD, HPNB/PMNT/RCDT/ESCT) ; `TRANSFER INMEDIATA | 77548434-PEREZ REGA S.A. | PROFORMA PF25-0100001` (1198.37 EUR, PENA/04)
- **CUSTOMER_REFUND**: `FP EMMA GRAY REFUND DUPLICATE P` (-361.30 GBP, NORT/NTRF) ; `VIR SEPA EMIS /A JUAN PÉREZ RUBIO /MOTIF REFUND DUPLICATE PAYMENT FA23012775 /REF ` (-4219.46 EUR, LORV/NTRF) ; `TRANSFERENCIA A PASCUAL Y CARMONA CAMP | ING S.L. | DEVOLUCION PAGO DUPLICADO FV24-0323` (-870.33 EUR, PENA/04)
- **FX_DEAL**: `FX DEAL 2109039996 EUR/USD` (250000.00 USD, PENA/13) ; `COMPRA DIVISA USD 1.000.000 OP 2402073 | 142` (-931514.48 EUR, PENA/13) ; `FX DEAL 2411225999 AUD/EUR` (-790000.00 AUD, WATT/FORX/SPOT/OTHR)
- **INSURANCE**: `INSURANCE PREMIUM` (-7665.91 EUR, RHEI/PMNT/IDDT/ESDD) ; `SEGUROS MULTIRRIESGO` (-9957.32 EUR, PENA/03) ; `SEGUROS MULTIRRIESGO` (-10655.95 EUR, RHEI/PMNT/IDDT/ESDD)
- **INTERCOMPANY**: `PRESTAMO IC VALTERMIA FRANCE SAS` (-250000.00 EUR, PENA/04) ; `TRANSF. IC VALTERMIA NORTH AMERICA INC | .` (4029000.00 USD, PENA/04) ; `TT IN  TRANSF. IC VALTERMIA INDUSTRIAL, S.A.` (14553996.34 CNY, LING/NTRF)
- **INTEREST_EXPENSE**: `LIQUIDACION INTERESES 4T21` (-8464.43 BRL, CAZU/NINT) ; `LIQUIDACION INTERESES 1T24` (-411.68 EUR, RIBA/17) ; `LIQUIDACION INTERESES 1T25` (-512.09 EUR, MONC/17)
- **INTEREST_INCOME**: `LIQUIDACION INTERESES 3T21` (339.67 EUR, LORV/NINT) ; `LIQUIDACION INTERESES 1T24` (39.33 GBP, NORT/NINT) ; `LIQUIDACION INTERESES 4T24` (3824.51 MXN, MEZQ/NINT)
- **INTERNAL_TRANSFER**: `TRASPASO A CTA 1447` (-2370000.00 USD, HPNB/CAMT/ACCB/OTHR) ; `TRASPASO A CTA 4863` (-200000.00 EUR, RIBA/04) ; `TRASPASO A CTA 1015` (-800000.00 EUR, RHEI/CAMT/ACCB/OTHR)
- **LEASING**: `CUOTA LEASING 1686` (-787.04 EUR, LORV/NDDT) ; `CUOTA ARRENDAMIENTO 5774` (-1596.70 AUD, WATT/PMNT/IDDT/ESDD) ; `CUOTA LEASING 3528` (-4350.38 CHF, RHEI/PMNT/IDDT/ESDD)
- **LOAN_DRAWDOWN**: `DISPOSICION POLIZA CREDITO 4430486` (8000000.00 EUR, PENA/99) ; `DISPOSICION POLIZA CREDITO 7497592` (4000000.00 EUR, PENA/99) ; `DISPOSICION POLIZA CREDITO 6696757` (9000000.00 EUR, PENA/99)
- **LOAN_REPAYMENT**: `CARGO POR AMORTIZACION DE PRESTAMO DE01-6279771428` (-16479.94 EUR, RHEI/LDAS/CSLN/PPAY) ; `CARGO POR AMORTIZACION DE PRESTAMO CN01-4956937604` (-473882.15 CNY, LING/NLDP) ; `CARGO POR AMORTIZACION DE PRESTAMO ES0 | 0-7347715973` (-34236.42 EUR, PENA/05)
- **PAYROLL**: `NOMINA 09 21 -` (-17725.05 MAD, LORV/NSAL) ; `NOMINA 02 24 -` (-2284.68 CHF, RHEI/PMNT/ICDT/SALA) ; `NOMINA 12 24 -` (-44829.56 MAD, LORV/NSAL)
- **SOCIAL_SECURITY**: `SOCIAL CHARGES` (-350788.08 MAD, LORV/NMSC) ; `SOCIAL CHARGES` (-1222752.87 BRL, CAZU/NMSC) ; `SOCIAL CHARGES` (-440257.66 PLN, RHEI/NMSC)
- **SUPPLIER_REFUND**: `TRANSFER INMEDIATA | 98204457-RT9 TRANSPORTES S.COOP. | REEMBOLSO FAC2100102` (191.99 EUR, PENA/04) ; `VIR SEPA RECU /DE ROBLES ENERGÍA SAS /MOTIF ABONO PAGO DOBLE /REF ` (2974.20 EUR, LORV/NTRF) ; `TRANSF. A SU FAVOR | 83594160-TRANSPORTES INDUSTRIAL S.L. | DEVOLUCION PAGO DUPLICADO` (11771.19 EUR, PENA/04)
- **TAX_PAYMENT**: `VAT RETURN` (-6475.38 EUR, VALD/NMSC) ; `TAX PAYMENT VAT` (-117109.09 CNY, LING/NMSC) ; `TAX AUTHORITY` (-24216.74 GBP, NORT/NMSC)
- **TAX_REFUND**: `DEVOLUCION AEAT MOD 303 202202` (719564.15 EUR, MONC/02) ; `DEVOLUCION AEAT MOD 303 202204` (686904.01 EUR, MONC/02) ; `DEVOLUCION AEAT MOD 303 202506` (717338.83 EUR, MONC/02)
- **UNIDENTIFIED**: `DIRECT CREDIT KAREN CAMPBELL PAYMENT` (900.00 AUD, WATT/PMNT/RCDT/ESCT) ; `INGRESO EFECTIVO OFICINA 0458 | REF 33136` (185.97 EUR, PENA/02) ; `TRANSFERENCIAS PONS HIDROTECNIA S.L. | PONS HIDRO PAGO` (181.88 EUR, MONC/04)
## Appendix: per-bank text samples (1-item lines)

**CAZU** (mt940)
```
text='LIQUIDACAO BOLETO 6775169327-5 BEM-ESTAR SOL EIRELI N/REF 2024-1276' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=NF0124001713 party='Bem-Estar Sol EIRELI' kind=ARI seg=ar_short_withholding
text='LIQUIDACAO BOLETO 8521140822-8 PAULA HERRERA RODRÍGUEZ PAG FT NF0123011582' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=NF0123011582 party='Paula Herrera Rodríguez' kind=ARI seg=ar_1to1
text='LIQUIDACAO BOLETO 3825206555-4 NOVA ESPERANÇA DO SUL REG FT NF0123017011' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=NF0123017011 party='Nova Esperança do Sul Rega S.A.' kind=ARI seg=ar_1to1
text='PIX RECEBIDO ETXEBERRIA PASCUAL ÁGUAS EIRELI ' | cpty='None' rinfo='None' e2e='None' ref1='PAY-2022-659557' | doc=NF0122005743 party='Etxeberria Pascual Águas EIRELI' kind=ARI seg=ar_ref_noise
```

**HPNB** (camt053)
```
text='ACH CREDIT DAVID COLLINS CCD PAYMENT ID:5491387191' | cpty='David Collins' rinfo='INV# 1072404559' e2e='NOTPROVIDED' ref1='HPNB2405107796649' | doc=1072404559 party='David Collins' kind=ARI seg=ar_1to1
text='ACH CREDIT AVA WRIGHT CCD PAYMENT ID:3396539058' | cpty='Ava Wright' rinfo='PAGO RECIBO 307041' e2e='NOTPROVIDED' ref1='HPNB2307116083356' | doc=1052307041 party='Ava Wright' kind=ARI seg=ar_dd_represented_transfer
text='ACH CREDIT AQUATICS OASIS L CCD PAYMENT ID:7664221308' | cpty='Aquatics Oasis LLC' rinfo='None' e2e='NOTPROVIDED' ref1='HPNB2305039939441' | doc=1102305712 party='Aquatics Oasis LLC' kind=ARI seg=ar_same_amount_fifo
text='RUECKLASTSCHRIFT AM04 MREF+VTM-US-0012228' | cpty='Lake Harmon Resort LLC' rinfo='None' e2e='None' ref1='HPNB2111153882059' | doc=1062112573 party='Lake Harmon Resort LLC' kind=ARI seg=ar_dd_returned
```

**LING** (mt940)
```
text='TT IN RJW WATER CARE CO., LTD. RJW' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=VTN230115250 party='RJW Water Care Co., Ltd.' kind=ARI seg=ar_same_amount_fifo
text='TT IN POOLS HAIZHOU CO., LTD. INVOICE VTN220101121' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=VTN220101121 party='Pools Haizhou Co., Ltd.' kind=ARI seg=ar_1to1
text='TT IN WATER CARE LEVANTE CO., LTD. SERVICE FEBRUARY 2022' | cpty='None' rinfo='None' e2e='None' ref1='20220502776550' | doc=VTN220102546 party='Water Care Levante Co., Ltd.' kind=ARI seg=ar_same_amount_month
text='TT OUT RP9 PUMPS CO., LTD. FRA F23/04175' | cpty='None' rinfo='None' e2e='None' ref1='PR010023060800-0' | doc=F23/04175 party='RP9 Pumps Co., Ltd.' kind=API seg=ap_payment_single
```

**LORV** (mt940)
```
text='/ORDP/JORGE FUENTES BERNAL/REMI/JORGE/EREF/NOTPROVIDED' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FA230107499 party='Jorge Fuentes Bernal' kind=ARI seg=ar_ref_noise
text='/ORDP/EDUARDO ZUBIRI FUENTES/REMI/REGLEMENT FACT FA210226272/EREF/NOTPROVIDED' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FA220232438 party='Eduardo Zubiri Fuentes' kind=ARI seg=ar_ref_noise
text='/ORDP/GLS SPA SAS/REMI/N/REF 2023-9220/EREF/NOTPROVIDED' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FA230201407 party='GLS Spa SAS' kind=ARI seg=ar_ref_noise
text='PRLV SEPA MA9 ASESORÍA EURL RUM 833921333345 N 2024110741762389' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FAC2400096 party='MA9 Asesoría EURL' kind=API seg=ap_direct_debit
```

**MEZQ** (mt940)
```
text='/ORDP/CAMPING FERRER E HIJOS S.A. DE C.V./REMI/' | cpty='None' rinfo='None' e2e='None' ref1='PAY-2024-651417' | doc=A01-2410109 party='Camping Ferrer e Hijos S.A. de C.V.' kind=ARI seg=ar_same_amount_fifo
text='SPEI RECIBIDO CLAVE 200125935909625102 REF 2203242 CONCEPTO PAGO FRA. 2306997 PHI INSTALACIONES S.A. DE C.V.' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=A01-2306997 party='PHI Instalaciones S.A. de C.V.' kind=ARI seg=ar_ref_noise
text='SPEI RECIBIDO CLAVE 457433043987022171 REF 5672404 CONCEPTO CUOTA MANTENIMIENTO FEBRERO SUMINISTROS RIERA 2000 S.A. DE' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=A01-2302249 party='Suministros Riera 2000 S.A. de C.V.' kind=ARI seg=ar_same_amount_month
text='SPEI RECIBIDO CLAVE 810617799818145377 REF 1191882 CONCEPTO  PAULA OTERO MARTÍNEZ' | cpty='None' rinfo='None' e2e='None' ref1='PAY-2021-534266' | doc=A01-2108466 party='Paula Otero Martínez' kind=ARI seg=ar_short_withholding
```

**MONC** (n43)
```
text='TRANSFERENCIAS PRIETO Y PASTOR DEPURA | PRIETO Y PASTOR DEPURACION S.C' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV24-0521946 party='Prieto y Pastor Depuración S.Coop.' kind=ARI seg=ar_ref_noise
text='TRANSFERENCIAS AMA AGUAS S.COOP. | AMA AGUAS  FRA. FV23-0505383' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV23-0505383 party='AMA Aguas S.Coop.' kind=ARI seg=ar_1to1
text='TRANSFERENCIAS CLIMATIZACION ARENAL D | CLIMATIZAC PAGO FRA. FV23-0121199' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV23-0121199 party='Climatización Arenal del Sur S.L.U.' kind=ARI seg=ar_1to1
text='TRANSFERENCIAS RIERA Y QUINTANA HOTEL | RIERA Y QUINTANA HOTELES S.A.' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV21-0405829 party='Riera y Quintana Hoteles S.A.' kind=ARI seg=ar_ref_noise
```

**NORT** (mt940)
```
text='FP BROOKS POOL BUILDE ' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=INV24-0121055 party='Brooks Pool Builders Ltd' kind=ARI seg=ar_ref_noise
text='FP POOL SUPPLY HOLLOW INV INV22-0121768' | cpty='None' rinfo='None' e2e='None' ref1='20230420604430' | doc=INV22-0121768 party='Pool Supply Hollowford Limited' kind=ARI seg=ar_instalment
text='CARD SETTLEMENT MID 328698493 2023-04-25' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=WEB23-9000373 party='WEB / POS CUSTOMERS' kind=ARI seg=ar_card_settlement
text='FP DE3 ELECTRONICS S. FRA A002791' | cpty='None' rinfo='None' e2e='None' ref1='PR010025050872-0' | doc=A002791 party='DE3 Electronics S.n.c.' kind=API seg=ap_payment_single
```

**PENA** (n43)
```
text='TRANSF. A SU FAVOR | 19559515-ALEJANDRO PARDO BOSCH | FACTURA FV23-0111145' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV23-0111145 party='Alejandro Pardo Bosch' kind=ARI seg=ar_1to1
text='TRANSFERENCIA SEPA | 13166693-ROSA SEGURA GIL | FACTURA FV22-0212783' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV22-0212783 party='Rosa Segura Gil' kind=ARI seg=ar_1to1
text='TRANSFER INMEDIATA | 87129300-MANTENIMIENTOS SIERRAMAYOR S. | L | 87129300-MANTENIMIENTOS SIERRAMAYOR S.' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV23-0200156 party='Mantenimientos Sierramayor S.L.' kind=ARI seg=ar_same_amount_fifo
text='PAGO TRANSFERENCIAS | 0971-56-2078951-40 876700 000118' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV-24-0007 party='Mantenimiento Redondo S.A.' kind=API seg=ap_payment_single
```

**RHEI** (camt053)
```
text='SEPA-UEBERWEISUNG EREF+NOTPROVIDED SVWZ+RE 205624' | cpty='Soriano Pardo Wellness e.K.' rinfo='RE 205624' e2e='NOTPROVIDED' ref1='RHEI2406110374507' | doc=RE240205624 party='Soriano Pardo Wellness e.K.' kind=ARI seg=ar_ref_noise
text='/TRTP/SEPA OVERBOEKING/NAME/WATER CARE MITCHELL PLUS B.V./REMI/INVOICE INV23-0101649/EREF/PAY-2023-355424' | cpty='Water Care Mitchell Plus B.V.' rinfo='INVOICE INV23-0101649' e2e='PAY-2023-355424' ref1='RHEI2303060771047' | doc=INV23-0101649 party='Water Care Mitchell Plus B.V.' kind=ARI seg=ar_1to1
text='/TRTP/SEPA OVERBOEKING/NAME/SANDERS LEISURE B.V./REMI/SANDERS/EREF/NOTPROVIDED' | cpty='Sanders Leisure B.V.' rinfo='SANDERS' e2e='NOTPROVIDED' ref1='RHEI2312078764773' | doc=INV23-0119865 party='Sanders Leisure B.V.' kind=ARI seg=ar_same_amount_fifo
text='SEPA-UEBERWEISUNG EREF+PAY-2021-843872 SVWZ+RE RE2102058' | cpty='Gallego Pooltechnik GmbH & Co. KG' rinfo='RE RE2102058' e2e='PAY-2021-843872' ref1='RHEI2110213690336' | doc=RE210205873 party='Gallego Pooltechnik GmbH & Co. KG' kind=ARI seg=ar_ref_noise
```

**RIBA** (n43)
```
text='TRANSFERENCIA DE JARDINS ALVORADA S.A. | CONCEPTO: PRESTACAO 2/2 FT INV24-01009 | 68' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=INV24-0100968 party='Jardins Alvorada S.A.' kind=ARI seg=ar_instalment
text='TRANSFERENCIA DE CONSTRUCCIONES LEVANT | E S.L.U. | CONCEPTO: CONSTRUCCIONES' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV23-0407737 party='Construcciones Levante S.L.U.' kind=ARI seg=ar_ref_noise
text='TRANSFERENCIA DE SPA HIDALGO GROUP S.L | .U. | CONCEPTO: TRANSFERENCIA | ORDENANTE ES4794256925145267459832' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV23-0110647 party='Spa Hidalgo Group S.L.U.' kind=ARI seg=ar_duplicate_payment
text='TRANSFERENCIA DE SUMINISTROS AGUIRRE E |  HIJOS S.A. | ORDENANTE ES5794472334140442935871' | cpty='None' rinfo='None' e2e='None' ref1='None' | doc=FV22-0200584 party='Suministros Aguirre e Hijos S.A.' kind=ARI seg=ar_ref_noise
```

**VALD** (mt940)
```
text='BONIFICO DA IMPIANTI MÁRQUEZ S.P.A. CAUSALE SALDO FT FT24-01-06922' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FT24-01-06922 party='Impianti Márquez S.p.A.' kind=ARI seg=ar_1to1
text='SDD RETURN AM04 MANDATE VTM-IT-0001336 ÓSCAR HERRERO LARA' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FT22-01-04651 party='Óscar Herrero Lara' kind=ARI seg=ar_dd_returned
text='BONIFICO DA GEG GIARDINI S.P.A. CAUSALE PAYMENT' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FT23-01-29812 party='GEG Giardini S.p.A.' kind=ARI seg=ar_ref_noise
text='BONIFICO DA SAN VITALE ALTO ACQUE S.N.C. CAUSALE PAYMENT' | cpty='None' rinfo='None' e2e='None' ref1='NONREF' | doc=FT21-01-07034 party='San Vitale Alto Acque S.n.c.' kind=ARI seg=ar_ref_noise
```

**WATT** (camt053)
```
text='DIRECT CREDIT POOLS WILSON PTY LTD ' | cpty='Pools Wilson Pty Ltd' rinfo='None' e2e='NOTPROVIDED' ref1='WATT2412245259801' | doc=INV24-0220327 party='Pools Wilson Pty Ltd' kind=ARI seg=ar_ref_noise
text='DIRECT CREDIT MIA SCOTT INVOICE INV22-0118' | cpty='Mia Scott' rinfo='INVOICE INV22-0118606' e2e='NOTPROVIDED' ref1='WATT2212088793176' | doc=INV22-0118606 party='Mia Scott' kind=ARI seg=ar_1to1
text='DIRECT CREDIT HBB BACKYARD PTY LTD INVOICE INV23-030' | cpty='HBB Backyard Pty Ltd' rinfo='INVOICE INV23-030' e2e='NOTPROVIDED' ref1='WATT2305017635962' | doc=INV23-0307432 party='HBB Backyard Pty Ltd' kind=ARI seg=ar_ref_noise
text='DIRECT CREDIT EDWARDS & BROWN POOL ' | cpty='Edwards & Brown Pool Supply Pty Ltd' rinfo='None' e2e='NOTPROVIDED' ref1='WATT2110186529701' | doc=INV21-0308766 party='Edwards & Brown Pool Supply Pty Ltd' kind=ARI seg=ar_same_amount_fifo
```
Scripts (analysis workspace): s*.py; derived tables: seg.csv, refs_by_bank.csv, refs_by_seg.csv, uniq_by_seg.csv, cat_top_keys.csv; parquet caches in pq/.
