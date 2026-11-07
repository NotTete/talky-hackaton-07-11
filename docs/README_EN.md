# Talky × Anthropic — Bank reconciliation at scale hackathon

**Grupo Valtermia** (fictitious) is a Spanish industrial group the size of an IBEX-35 company: it designs, manufactures
and distributes equipment, spare parts, chemicals and services for water installations (pools, wellness, irrigation).
17 legal entities in 15 countries (one more in FINAL), 12 currencies, 13 banks, ~50 accounts, tens of thousands of customers — from chains
that pay hundreds of invoices with one transfer to installers paying by direct debit — and a treasury team that gets
hundreds of thousands of bank lines every half-year.

**Your challenge:** for every bank line, say which open items (invoices, credit notes) it settles and for how much, and —
when it settles none — what it is (fee, payroll, cash-pool sweep, tax…), flagging exceptions (partial payments,
early-payment discounts, bank charges, withholding, FX differences, returns, duplicates, unidentified receipts…).

Everything is **synthetic**: no company, bank, person, tax id or IBAN is real (IBANs have valid check digits on invented
bank codes). The one exception is `masters/fx_rates.csv`: those are the real ECB euro reference rates (the Moroccan dirham, which the ECB does not publish, is derived from the Bank Al-Maghrib 60 % EUR / 40 % USD basket). Volumes, formats and patterns are calibrated on public figures of a real group and on patterns abstracted
from real statements.

## Phases

| Phase | Dates | You get |
|---|---|---|
| TRAIN | 2021-07-01 → 2025-06-30 (4 years) | inputs **and** labels (`train/golden_allocations.csv.gz`) |
| LIVE | 2025-07-01 → 2025-12-31 | inputs from the start; live leaderboard |
| FINAL | 2026-01-01 → 2026-06-30 | inputs near the end; **one scored submission** per team; final leaderboard |

There is deliberate drift: a bank migrates from Norma 43 to camt.053 in October 2025, a big customer changes its
remittance layout, a subsidiary is acquired in FINAL (South Africa, ZAR), customers are renamed, change banks or pay later.

## What to submit

One CSV per phase, one row per allocation — see **FORMAT.md**. Score = 100 × (0.5 · allocation F1 + 0.3 · transaction
accuracy + 0.2 · exception macro-F1).

- **LIVE**: submit as often as the limits allow; the live leaderboard shows your score.
- **FINAL**: **one scored submission per team** — no re-submits and no choosing among several files. A file rejected by
  validation does not count (fix it and upload again). The score stays hidden until the reveal at 20:30; the final ranking
  is that single FINAL score, and ties go to the earlier submission. Validate your file on LIVE/TRAIN first.

## Getting started

1. **POLICIES.md** — the rules the labels follow exactly (tolerances, discounts, when a row is `PARTIAL`, FX, returns,
   what `UNIDENTIFIED` means…).
2. **DATA_DICTIONARY.md** — every file and column.
3. `sample/` — one TRAIN month with its labels.
4. `starter/starter.py` — loads the data, does a naive match (exact amount + invoice number in the text) and writes a
   valid LIVE submission; `starter/bankrec_score.py` is the server's scorer, so you can score yourself on TRAIN.

## Why it is hard

Invoice numbers are rarely verbatim in bank texts (the truncated payer name is the main key); one receipt can pay 300
invoices with an advice that arrives days later; one SEPA batch of 400 receipts is a single bank line; key context lives
only in PDFs ("instalment 2 of 3", "cancels and replaces…", "5 % retention", "less advance…", "May services"); banks
convert currencies at their own rate and charge fees in separate lines; some receipts have no invoice at all and must be
flagged rather than forced. A LIVE half-year has ~210k bank lines and ~750k allocations over almost a million open items.

Good luck!
