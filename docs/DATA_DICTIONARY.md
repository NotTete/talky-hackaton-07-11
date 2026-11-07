# Data dictionary

Everything is synthetic. Dates are ISO `YYYY-MM-DD`; amounts are decimal with a dot, 2 decimals, in the currency of the
row. Big tables are `.csv.gz` (UTF-8, comma, header).

```
masters/                     state of the master data at 2025-12-31 (FINAL brings final/masters_final/ at 2026-06-30)
train/  2021-07-01 … 2025-06-30   inputs + golden_allocations.csv.gz
live/   2025-07-01 … 2025-12-31   inputs + open_items_at_start.csv.gz (no golden)
final/  2026-01-01 … 2026-06-30   inputs + open_items_at_start.csv.gz + masters_final/ (released at the end)
sample/ one month (2025-06) of TRAIN with its golden, for a quick start
starter/ naive matcher that writes a valid LIVE submission + the reference scorer
```

Every phase folder has the same structure:

| Path | Content |
|---|---|
| `bank_statements/<account_id>/<account_id>_<YYYYMM>.<n43\|sta\|xml>` | **Raw statements** as the bank delivers them: Norma 43 (`.n43`, 80-byte records, latin-1, CRLF), SWIFT MT940 (`.sta`), ISO 20022 camt.053.001.08 (`.xml`). One file per account and month. Banco del Moncayo switches from N43 to camt.053 on 2025-10-01. Banca Ribagorza's exports **overlap** (each file repeats the last days of the previous month): duplicate raw rows are expected there, the normalised table is de-duplicated. |
| `bank_transactions.csv.gz` | **Normalised view** of the same lines, one row per bank line (no duplicates). Columns below. |
| `ar_invoices.csv.gz` | Our customer invoices issued in the period (TRAIN also includes invoices issued before 2021-07 that were still open). |
| `ap_invoices.csv.gz` | Supplier invoices received in the period. |
| `credit_notes.csv.gz` | AR and AP credit notes issued in the period. |
| `open_items_at_start.csv.gz` | (LIVE/FINAL) items still open at the end of the previous phase and their open amount. |
| `remittance_advices/<entity>/RA_*.{pdf,csv,xlsx,txt}` + `index.csv` | Payment advices sent by customers (mostly key accounts). `index.csv`: file, entity, payer_name, received_date, format. The advice may arrive after the cash. |
| `direct_debit_batches/<entity>_<branch>_<YYYYMM>.csv` | Our SEPA / BACS / ACH direct-debit collection files (one row per receipt). |
| `card_settlements/<entity>_<YYYYMM>.csv` | Acquirer settlement detail (one row per card order). |
| `cheque_deposits/<entity>_<branch>_<YYYYMM>.csv` | Cheque / lockbox deposit detail (US lockbox, FR/MA remises de chèques). |
| `payment_runs/<entity>_<YYYYMM>_<n>.csv` | Our outgoing payment files (transfers, aggregated files, confirming remittances), one row per document paid. |
| `pdfs/<entity>/*.pdf` + `index.csv` | PDFs of a **subset** of documents: invoices and credit notes (ours and suppliers'), proformas, bank-account change notices. Free-text clauses in the PDF (instalments, retention, advance to deduct, "anula y sustituye", service month, rappel offset vs paid out, credit-note refund vs offset, factoring assignment, new IBAN) are **not** in any CSV. `index.csv`: file, document_type, document_number, entity, party_id, document_date. |
| `golden_allocations.csv.gz` | (TRAIN only) the labels: `bank_txn_id, item_id, amount, category, exception, segment, difficulty`. `segment` names the kind of case, `difficulty` 1–3. |

## bank_transactions.csv.gz

| Column | Meaning |
|---|---|
| `bank_txn_id` | Stable id of the bank line, unique in the whole dataset (`BT-<year>-<7 digits>`), ordered by booking date. Use it in submissions. |
| `account_id` | Our bank account (see `masters/bank_accounts.csv`). |
| `entity`, `bank` | Owner entity of the account and bank code. |
| `statement_format` | `n43`, `mt940` or `camt053` (format of the raw file the line comes from). |
| `statement_file` | Raw file that contains the line. |
| `booking_date`, `value_date` | Fecha operación / fecha valor (N43), entry/value date (MT940), BookgDt/ValDt (camt). |
| `amount` | Signed amount in the account currency (+ credit / − debit). |
| `currency` | Account currency. |
| `bank_code` | N43 concepto común (2 digits) · MT940 transaction type (`NTRF`, `NDDT`, `NCHG`…) · camt BkTxCd `Domain/Family/SubFamily`. Banks use them inconsistently. |
| `bank_subcode` | N43 concepto propio (bank-specific) · camt proprietary code (Banco del Moncayo keeps its N43 code there). |
| `ref1`, `ref2` | N43 Referencia 1 / Referencia 2 (16 chars) · MT940 customer reference / bank reference · camt entry reference. |
| `text` | Free text as printed by the bank: N43 record-23 fields (38 chars each, joined with ` \| `; words can be cut at the boundary) · MT940 `:86:` (65-char lines joined) · camt `AddtlNtryInf`. |
| `counterparty_name`, `counterparty_account` | camt only: ordering party / beneficiary name and IBAN (N43 and MT940 carry them, when at all, inside `text`). |
| `end_to_end_id` | camt only: EndToEndId. |
| `remittance_info` | camt only: unstructured remittance (`RmtInf/Ustrd`, 140 chars). |
| `orig_amount`, `orig_currency` | Instructed amount when the operation currency differs from the account currency (N43 record 24, camt InstdAmt). |

## Items

`ar_invoices.csv.gz`: `item_id, entity, document_number` (printed number, what customers quote), `issue_date, due_date,
customer_id, customer_name` (name printed on the invoice), `currency, net_amount, tax_amount, total_amount,
payment_terms` (code in `payment_terms.csv`), `customer_po` (the customer's order number, when given), `payment_method`
(`transfer`, `dd` direct debit, `card`, `cheque`).

`ap_invoices.csv.gz`: `item_id, entity, supplier_id, supplier_name, supplier_invoice_number` (the supplier's printed
number), `issue_date, due_date, currency, net_amount, tax_amount, total_amount, payment_terms, our_po, payment_method`
(`transfer`, `dd`).

`credit_notes.csv.gz`: `item_id, side` (AR/AP), `entity, document_number, issue_date, party_id, party_name, currency,
net_amount, tax_amount, total_amount` (positive), `related_document` (invoice it rectifies, when any), `reason`.

`open_items_at_start.csv.gz`: `item_id, item_type, entity, party_id, document_number, issue_date, due_date, currency,
total_amount, open_amount`.

Item ids (`ARI-`, `ARC-`, `API-`, `APC-` + issue year + 7 digits) carry no meaning beyond uniqueness.

## Batch and payment files

`direct_debit_batches`: `batch_id, scheme, collection_date, account_id, currency, batch_total, end_to_end_id, mandate_id,
debtor_name, debtor_iban, amount, remittance_info, re_presentation` (Y when the receipt was returned before and is
collected again).

`card_settlements`: `settlement_id, acquirer, merchant_id, settlement_date, account_id, currency, order_number, card,
auth_code, operation_date, gross, fee, net`.

`cheque_deposits`: `deposit_id, deposit_date, account_id, currency, deposit_total, cheque_number, drawer,
drawer_bank_routing, amount, stub_reference` (what was written on the payment stub, often incomplete).

`payment_runs`: `run_id, run_type` (`transfers` = one bank debit per beneficiary, `aggregated` = one bank debit for the
whole file, `confirming` = one debit per maturity date), `execution_date, debtor_account_id, end_to_end_id, beneficiary,
beneficiary_iban, currency, transfer_amount, document_number, document_type, document_amount` (negative for credit notes
netted), `remittance_info`.

## Masters

| File | Columns |
|---|---|
| `entities.csv` | entity, legal_name, country, functional_currency, role, in_group_since |
| `bank_accounts.csv` | account_id, entity, bank, bank_name, bic, currency, iban, account_number, purpose, statement_format, statement_format_from_2025_10_01, opened |
| `customers.csv` | customer_id, entity, name (current), trade_name, customer_type (KEY_ACCOUNT, DISTRIBUTOR, PROJECT, INSTALLER, INSTALLER_DD, WEB_CARD), country, tax_id, iban (known paying / mandate IBAN), sepa_mandate_id, payment_terms, invoice_currency, group_id, parent_customer_id, created, blocked_since |
| `suppliers.csv` | supplier_id, entity, name, country, tax_id, supplier_type, iban, account_number, payment_method, payment_terms, invoice_currency, created, blocked_since |
| `categories.csv` | category, description, gl_account — the allowed `category` values |
| `payment_terms.csv` | code, days, early_payment_discount_pct, discount_days, description |
| `fx_rates.csv` | date, currency, units_per_eur, source — daily reference rates, units of currency per 1 EUR, ECB/TARGET business days only (weekends and holidays: use the last available rate). `source` = `ECB` for the real ECB euro reference rates (USD, GBP, PLN, CHF, MXN, BRL, CNY, AUD, CAD, ZAR, as published, same values as e.g. the Frankfurter API); `BAM-basket (derived)` for MAD, which the ECB does not publish: computed from the real EUR/USD with the Bank Al-Maghrib basket (60 % EUR, 40 % USD, ±5 % band), so it is realistic but not an official fixing. |

Masters are a snapshot: renamed customers show their current name, IBAN changes are not always reflected.
