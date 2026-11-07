# Grupo Valtermia — matching and accounting policies (what the golden follows)

These are the rules the Treasury / Cash-application team of Grupo Valtermia applies. The labels
(`golden_allocations.csv.gz` in `train/`, and the hidden golden of LIVE and FINAL) follow them **exactly**. Where a
rule is not written here, the TRAIN history shows how it has always been done.

All amounts in this document are in the currency of the document (the *item*), unless stated.

---

## 1. What a reconciliation row means

For every bank line (`bank_txn_id`) you say which open items it settles and how much of each:

| Case | Rows |
|---|---|
| The line settles one or more items | one row per item: `item_id`, `amount` = amount of that item cleared **in the item currency**, always **positive** (credit notes too), `category` empty |
| The line settles no item | exactly one row: `item_id` and `amount` empty, `category` from `categories.csv` |

`exception` (one value per row, closed list) explains why the cash differs from the documents. Empty = no exception.

**Open items** are AR invoices (`ARI-…`), AR credit notes (`ARC-…`), AP (supplier) invoices (`API-…`) and AP credit notes
(`APC-…`). Proformas, remittance advices, payment-run files, notices… are **not** items.

## 2. The amount of each row

- **Normal payment**: amount = the open amount of the item (cleared in full).
- **Credit note netted in a payment** (customer pays invoices minus a credit note; or we pay a supplier net of its credit
  note): the credit note is a row with its full amount (positive) and no exception.
- **Full clearing with an explained difference** (the item is fully cleared even though less cash arrived):
  `SHORT_DISCOUNT`, `SHORT_BANK_CHARGES`, `SHORT_WITHHOLDING`, `FX_DIFFERENCE`, `OVERPAYMENT`
  → amount = **full open amount** of the item.
- **Not cleared in full** (`PARTIAL`, `SHORT_DISPUTE`) → amount = **what was actually applied** (the cash for that item).
- `DUPLICATE_PAYMENT` → amount = what was paid again (in item currency).
- `RETURNED` → amount = the amount that is re-opened (the amount of the original allocation being reversed).

Tolerance used by the scorer: an amount is correct when it is within max(0.01, 0.5 % of the golden amount).

## 3. Exceptions (closed list)

| Code | When |
|---|---|
| *(empty)* | Paid as documented. Also: the **last** instalment of a plan, the release of a retention, the remainder after a partial payment, the settlement of a disputed amount, re-presented direct debits, credit notes netted. |
| `PARTIAL` | After this line the item **still has an open balance** and no specific reason below applies. This includes: unplanned partial payments ("A CUENTA"), every instalment of an instalment plan except the last, the first payment of an invoice with a guarantee retention, the payment of an invoice whose PDF says an advance (proforma) must be deducted, and an early-payment discount taken **after** the deadline (the discount is not accepted, the difference stays open). |
| `SHORT_DISCOUNT` | Early-payment discount taken **within terms**: the payment terms of the invoice (`payment_terms.csv`) have a discount, the bank **booking date** is ≤ invoice date + discount days **+ 3 days of grace**, and the cash = invoice total − discount % × invoice **total** (rounded to the cent). The invoice is fully cleared. |
| `SHORT_BANK_CHARGES` | Cross-border transfer from outside SEPA where an intermediary bank deducted its fee (SHA/BEN): shortfall ≤ **60 EUR equivalent** and ≤ **2 %** of the item. Fully cleared. (Above the tolerance it would be `PARTIAL`.) |
| `SHORT_WITHHOLDING` | The customer withheld tax at source (some customers in Mexico, Brazil and Morocco; rates 1.25 %, 1.5 % or 10 % of the invoice total). Fully cleared — the tax certificate covers the difference. |
| `SHORT_DISPUTE` | The customer deducted an amount **with a stated reason** (price difference, damaged goods, short delivery, late-delivery penalty, unagreed co-op marketing, logistics fee…), usually visible only in its remittance advice. Amount = what was paid; the disputed part stays open (it may be paid later → that later line has no exception). |
| `OVERPAYMENT` | The customer paid more than the open amount of a single invoice. The invoice is fully cleared (amount = open amount); the excess stays on the customer account (no extra row). |
| `DUPLICATE_PAYMENT` | The item had already been settled and is paid again (customer pays twice; or we pay a supplier invoice twice). The later line gets the exception. A later refund of the duplicate is `CUSTOMER_REFUND` / `SUPPLIER_REFUND` (category). |
| `RETURNED` | A collection or payment comes back and re-opens the item: SEPA/BACS/ACH direct-debit return (R-transaction, any reason code), returned cheque, card chargeback, our transfer to a supplier rejected by the beneficiary bank. Every item of the returned operation gets the row. |
| `UNIDENTIFIED` | A receipt for which **no open item exists** (unknown payer, wrong beneficiary, cash deposit…). One row: category `UNIDENTIFIED`, exception `UNIDENTIFIED`. Do not force a match: some unidentified receipts have exactly the amount of an unrelated open invoice. |
| `FX_DIFFERENCE` | The bank line is in a **different currency** than the invoice (e.g. a USD invoice paid into a EUR account, an EUR invoice paid into a PLN account, or our USD supplier invoice paid from a EUR account). The payer pays the invoice amount in the invoice currency (the bank shows the original amount and/or rate) and the bank converts at its own corporate rate, which is worse than the reference rate in `fx_rates.csv` by a spread of roughly 0.1–0.6 % for G10 currencies (USD, GBP, CHF, CAD, AUD, PLN) and 0.4–1.5 % when an emerging-market currency is involved (MXN, BRL, CNY, ZAR, MAD) — always well within the 3 % FX tolerance. `fx_rates.csv` holds the real ECB euro reference rates (TARGET business days; on weekends and holidays use the last available rate); MAD, which the ECB does not quote, is derived from the Bank Al-Maghrib basket (60 % EUR / 40 % USD). Every **invoice** row of such a line that is fully cleared carries `FX_DIFFERENCE` (it replaces an empty exception, `SHORT_DISCOUNT`, `SHORT_WITHHOLDING` or `SHORT_BANK_CHARGES`); `PARTIAL` / `SHORT_DISPUTE` keep their code; credit-note rows stay empty. Amount = full open amount in invoice currency. |

## 4. Lines that settle no item (`category`)

See `masters/categories.csv`. Key rules:

- **Bank fees** (`BANK_FEES`): maintenance/settlement fees, per-transfer fees, international transfer fees (usually a
  **separate line the same day**), SEPA batch fees, return fees, card terminal fees.
- **Card acquirer settlements** are *not* fees: a settlement line clears the web/branch card orders at **gross** (see
  `card_settlements/`); the acquirer fee deducted is contractual and is not an exception.
- **Cash pool** (`CASH_POOL`): zero-balancing sweeps between participant accounts and the header (both sides).
- **Own-account transfers** of the same entity (`INTERNAL_TRANSFER`), **intercompany financing** between group entities
  (`INTERCOMPANY`), **FX deals** (`FX_DEAL`, both legs), payroll, social security, taxes, tax refunds, loans, leasing,
  insurance, corporate card statements, interest.
- **Bank error and its reversal** (`BANK_REVERSAL`): when the bank books a wrong movement and then cancels it
  ("ANULACION OPERACION INDEBIDA"), **both** lines are `BANK_REVERSAL`.
- **Payment of a proforma** (`CUSTOMER_ADVANCE`): a proforma is not an invoice; when a customer pays a proforma the
  invoice does not exist yet. The final invoice later says "a deducir anticipo…" and its payment is `PARTIAL` (§3).
- **Refund to a customer** of a duplicate payment (`CUSTOMER_REFUND`); **refund from a supplier** of a payment we made
  twice (`SUPPLIER_REFUND`). A supplier refund of one of **its credit notes** is not a category: it settles the `APC`
  item.

## 5. How to pick the items

1. **Who paid is not always the customer.** A parent company may pay for its subsidiaries (group in `customers.csv`:
   `group_id`, `parent_customer_id`), a confirming bank / factor may pay for the customer ("CONFIRMING … ORD. <customer>"),
   a collection agency may pay old invoices ("EXPTE …"). Allocate to the invoices of the customer that was paid for.
2. **Payment factory.** Valtermia Industrial (ES00) pays some supplier invoices of ES01, ES02 and PT01 from its own
   accounts. The items are those of the subsidiary.
3. **Batches.** A SEPA/BACS/ACH direct-debit batch credit clears all receipts listed in `direct_debit_batches/` for that
   batch; a card settlement clears the orders in `card_settlements/`; a cheque/lockbox deposit clears the cheques in
   `cheque_deposits/`; an aggregated payment-run or confirming debit clears all documents of the run in
   `payment_runs/`; a per-transfer debit clears the documents of that transfer (`end_to_end_id`).
4. **Remittance advices.** Big customers send an advice (PDF, CSV, XLSX or e-mail text) that lists the documents paid,
   deductions and reasons. It can arrive a few days after the cash, can use the customer's own voucher numbers and our
   invoice number without prefix (one big customer changes layout in September 2025), and the bank text only carries the
   customer's payment reference.
5. **References are noisy.** Expect our number without prefix or leading zeros, typos and transposed digits, truncation at
   16/35/38 characters, the customer's PO number instead of ours, an old (cancelled) invoice number, or no reference at all
   (name or IBAN only). The party name in bank texts is often truncated or a trade name; customers are renamed (M&A /
   rebranding) and the master shows the current name.
6. **Same amount, several open invoices** (fixed monthly maintenance fees): without a usable reference the customer
   pays the **oldest open** invoice (FIFO by invoice date). When the text names a month ("MANT. MAYO 25", "SERVICIOS
   MAYO") it pays the invoice of **that service month** (the invoice PDF names the month; monthly fees are invoiced at the
   beginning of the following month).
7. **Cancelled and replaced invoices.** A PDF "anula y sustituye a la factura X" means X was cancelled by a credit note and
   replaced: a payment citing X settles the **replacement** invoice. X and its cancelling credit note are never settled
   by bank.
8. **Instalments, retentions, advances.** Only the invoice PDF says that an invoice is paid in instalments ("Forma de pago:
   3 vencimientos…"), that 5–10 % is retained as a guarantee until final acceptance (paid ~12 months later), or that an
   advance received against a proforma must be deducted. See §3 for the exceptions.
9. **Credit notes.** Return/price credit notes reference their invoice (`related_document`). Rappel (volume rebate)
   credit notes are quarterly; their PDF says whether they will be **offset** in the next payment or **paid out** by
   transfer (then our outgoing transfer settles the `ARC` item). Supplier credit notes are either offset in our next
   payment run or refunded by the supplier (PDF).
10. **Returned direct debits** are often re-presented in a later batch (the batch file flags `re_presentation=Y`) or the
    customer pays by transfer afterwards.
11. **New bank details.** Customers sometimes notify a new paying account (PDF notice); IT01 changed its collections
    account on 2026-02-01 and some customers keep paying into the old one for months — both are valid.

## 6. Drift you should expect between TRAIN, LIVE and FINAL

- Banco del Moncayo migrates its statements from Norma 43 to camt.053 on **2025-10-01** (texts look different).
- A big customer switches its remittance-advice layout in **September 2025**.
- A new subsidiary is acquired on **2026-01-01** (ZA01, new bank, new currency ZAR, own invoice numbering "KWI…"), and
  renamed on 2026-04-01; updated masters come with the FINAL data (`final/masters_final/`).
- Customers come and go, are renamed and change banks; payment behaviour gets slower in 2026.

## 7. Not in scope

No journal entries are needed; FX gains/losses are not to be computed — only the `FX_DIFFERENCE` flag. Each bank line
belongs to exactly one phase (by booking date).
