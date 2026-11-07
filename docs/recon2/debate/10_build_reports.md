# Build phase — the six builders' reports (summarised faithfully)

## Data layer (money, textviews, fx, load, book)
19 tests pass. Sample 8,499 lines / 65,846 items: load 0.9 s, book 0.7 s. LIVE 203,083 lines /
1,031,576 items: load 16.4 s, book 12.4 s. 325,419 carried items rejoined to TRAIN (only the 27,529
credit notes lack terms, by design); 32,091 residue items. money() uses string arithmetic only; "1.023"
raises (ambiguous). Tokens: glued splits, continuation lists. FX as exact fractions. Book: supplier-
qualified AP keys, prefix/suffix lookups (>64 hits without scope → ambiguous), apply() semantics per
exception (RETURNED re-opens, DUP leaves the balance, PARTIAL/SD subtract, others close).

## Documents (advices, pdfclauses)
15 tests. Advices: CSV `,`/`;`, TXT (any Subject token as ref; EN mm/dd, ES dd/mm), PDF, XLSX old and
Sep-2025 layouts; LIVE 6,889 advices, refs 100 %, Σpaid = stated 97.98 % (the rest are real
underpayments), rows parsed = raw rows on every file; 6,732 link to a LIVE line. PDF clauses: every LIVE
family count exact (1308 / 916 / 276 / 1694 / 565 / 940+409 / 884+510 / 352 / 1497 / 79+79);
instalments sum to total 1308 / 1308, retention + payable = total 916 / 916.

## Routing (treasury, cats, party)
Treasury 19,244 / 19,250 on TRAIN, 0 FP on 1.49 M lines (~5 µs/line). Router 0.9999 on 2025 category
lines. Party recall 0.897 overall (ar_ref_noise 0.964), average set 3.66, 32 µs/line. Third-party and
factor lines are party-free by design.

## Accounting (decide + oracle)
Oracle (true items, true balances): every class F1 ≥ 0.95 except PARTIAL 0.968 with advice; without
advice SHORT_DISPUTE is 0 (cannot be inferred from arithmetic) and BR01 withholding needs the RET rows.
Sample macro-F1 0.811 base / 0.995 with advice facts; full TRAIN 0.833 / 0.994. Gaps noted: multi-
invoice discount windows, instalments whose total divides evenly, several partials per line.

## Ops (dd, cards, cheques, runs, returns)
Sample: exact item sets on dd 72/72, cards 84/84, cheques 41/41, payment_multi 787/787,
single 458/458, credit-note netted 97/97, factored 31/33, confirming 42/67, dd_returned 199/202,
cheque returned 3/3, chargeback 3/3, AP returned transfer 1/1; 0 wrong hypotheses elsewhere. LIVE:
39,760 lines get an ops hypothesis; resolve 20.5 s.

## Integrator (resolve, engine, writer, trace, gap, CLI)
First end-to-end sample: **87.70** (F1 0.896, accuracy 0.928, macro 0.752); without the sample carry-over
86.10; old engine 78.65. Deterministic (two runs, same sha). Subset search moved to a bitset (40 s → 5 s
on the sample). Top remaining losses: ar_short_dispute 1.97, ar_1toN_credit_note 1.45, ar_ref_noise
0.62, ap_confirming 0.55, ar_n_to_m 0.39; weakest classes OVERPAYMENT 0.39, SW 0.55, UNIDENTIFIED 0.47.
