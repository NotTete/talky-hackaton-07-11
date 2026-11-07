# Submission format / Formato de entrega

One CSV file per phase (LIVE or FINAL), UTF-8, comma separated, with header, optionally gzip-compressed (`.csv.gz`).
One row per allocation. Limits: **≤ 60 MB uncompressed, ≤ 3,000,000 rows**.

Un CSV por fase (LIVE o FINAL), UTF-8, separado por comas, con cabecera, opcionalmente comprimido (`.csv.gz`). Una fila por
asignación. Límites: **≤ 60 MB sin comprimir, ≤ 3.000.000 filas**.

```
bank_txn_id,item_id,amount,category,exception
BT-2025-0001234,ARI-2025-0456789,1210.00,,
BT-2025-0001234,ARI-2025-0456790,605.00,,
BT-2025-0001234,ARC-2025-0012345,121.00,,
BT-2025-0001240,,,BANK_FEES,
BT-2025-0001300,ARI-2025-0400001,980.00,,SHORT_DISCOUNT
BT-2025-0001301,,,UNIDENTIFIED,UNIDENTIFIED
BT-2025-0001302,ARI-2025-0400002,400.00,,PARTIAL
BT-2025-0001310,API-2025-0077777,15300.00,,FX_DIFFERENCE
```

| Column | Rule |
|---|---|
| `bank_txn_id` | A bank line of the phase you submit (`live/bank_transactions.csv.gz` or `final/…`). Lines you omit count as wrong. |
| `item_id` | An open item id (`ARI-`, `ARC-`, `API-`, `APC-` + year + 7 digits). Empty for lines that settle no item. |
| `amount` | Amount of the item cleared by this bank line, **in the item currency**, positive, dot as decimal separator (`1210.00`). Empty when `item_id` is empty. See POLICIES §2. |
| `category` | Only for lines that settle no item: one code of `masters/categories.csv`. Must be empty on item rows. |
| `exception` | Empty or one of: `PARTIAL`, `OVERPAYMENT`, `SHORT_DISCOUNT`, `SHORT_BANK_CHARGES`, `SHORT_WITHHOLDING`, `SHORT_DISPUTE`, `DUPLICATE_PAYMENT`, `UNIDENTIFIED`, `RETURNED`, `FX_DIFFERENCE`. |

Categories (`masters/categories.csv`): `BANK_FEES`, `INTEREST_INCOME`, `INTEREST_EXPENSE`, `PAYROLL`, `SOCIAL_SECURITY`,
`TAX_PAYMENT`, `TAX_REFUND`, `LOAN_DRAWDOWN`, `LOAN_REPAYMENT`, `LEASING`, `FX_DEAL`, `CASH_POOL`, `INTERCOMPANY`,
`INTERNAL_TRANSFER`, `CUSTOMER_ADVANCE`, `CUSTOMER_REFUND`, `SUPPLIER_REFUND`, `CARD_EXPENSES`, `INSURANCE`,
`BANK_REVERSAL`, `UNIDENTIFIED`.

## Validation (the upload is rejected with row numbers if any fails)

- header must contain exactly the five columns (any order); `;` as separator is accepted (then `,` decimals too);
  BOM, CRLF and surrounding spaces are tolerated;
- every `bank_txn_id` must belong to the phase;
- no duplicated `(bank_txn_id, item_id)` pair, and at most one no-item row per bank line;
- a bank line cannot have both item rows and a category row;
- `amount` required (non-negative number) when `item_id` is given, empty otherwise;
- `category` and `exception` must be in the lists above.

## Scoring / Puntuación

Over all bank lines T of the phase:

- **Allocation F1 (50 %)**: pairs `(bank_txn_id, item_id)`; a submitted pair is a true positive when the golden has it
  and the amount is within max(0.01, 0.5 % of the golden amount). Precision = TP / submitted pairs, recall = TP / golden
  pairs.
- **Transaction accuracy (30 %)**: a bank line is correct when its whole set of (item, amount ± tolerance, exception)
  equals the golden, or — for lines with no item — its category and exception equal the golden.
- **Exception macro-F1 (20 %)**: per row key (bank line, item or empty), F1 of each non-empty exception class present in
  the golden, averaged.
- **Score = 100 × (0.5 · F1 + 0.3 · accuracy + 0.2 · macro-F1)**, two decimals. The result page also shows precision,
  recall, amount-weighted accuracy (share of |amount| in EUR of correctly reconciled lines) and accuracy per segment.

## FINAL: one scored submission / FINAL: un único envío puntuado

- **LIVE**: as many submissions as the limits allow; the live leaderboard shows your score.
- **FINAL**: **one scored submission per team**. No re-submits and no choosing among several files. A file rejected by
  validation (rules above) does not count, so you can fix it and upload again. The score stays hidden until the
  **20:30 reveal**; the final ranking is that single FINAL score, and ties go to the earlier submission.

- **LIVE**: tantos envíos como permitan los límites; el ranking en vivo muestra tu puntuación.
- **FINAL**: **un único envío puntuado por equipo**. Sin reenvíos y sin elegir entre varios ficheros. Un fichero rechazado
  por la validación (reglas de arriba) no cuenta, así que puedes corregirlo y volver a subirlo. La puntuación queda oculta
  hasta la **revelación de las 20:30**; el ranking final es esa única puntuación de FINAL y los empates se resuelven a favor
  del envío más temprano.

You can score yourself on TRAIN: `starter/` writes a valid file and the reference scorer is published in
`starter/bankrec_score.py` (the same code runs on the server).
