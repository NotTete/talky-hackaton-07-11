# Reference implementation (not the starting point)

A fuller version of the pipeline, kept for reference. On the 1-month sample it scores
**~31** with the official scorer, versus **~20** for the root scaffold (`src/bankrec/`).

What it adds over the root scaffold:

| file | what it does |
|---|---|
| `bankrec_impl/advices.py` | parses remittance advices (csv / txt, pdf hook) and uses their documents, deductions and reasons (this is where `ar_short_dispute` comes from) |
| `bankrec_impl/candidates.py` | blocking with batch/run document expansion (direct debits, card settlements, cheque deposits, payment runs), IBAN/name party resolution, advice lookup |
| `bankrec_impl/items.py` | item book with running balances, reference/party/amount indexes (bisect over sorted amounts), FX conversion |
| `bankrec_impl/match.py` | subset-sum allocation, partial payments, duplicate/returned detection |
| `bankrec_impl/rules.py` | discount / withholding / bank-charge / dispute rules, category keyword table |

It predates the polars data layer and expects its own `Phase`/`Masters` API, so it
will not run as-is here; treat it as a source of ideas. Treat it as a source of ideas, not a dependency.
