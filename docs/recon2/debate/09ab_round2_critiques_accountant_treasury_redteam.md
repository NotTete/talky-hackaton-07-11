# ROUND 2 — critiques of proposal v2 (accountant, treasury) and the time decision

Transcribed from the debate record. Documents round 2 is in `09c_round2_critique_documents.md`.

## Accountant — round 2: ACCEPT-WITH-CHANGES
v2 fixes most round-1 blockers (refunds, residues, n_to_m selector, DUP E2, SBC gate, RET → SW,
OVERPAYMENT on first payment only). Two blockers remain in the decide table.

**BLOCKER 1 — E7/E8 are written per row, but most discount/withholding rows are on multi-invoice
lines, where the per-row short is not observable.**
* SW: 25,146 of 33,818 rows (74 %) are on 1,538 multi-item lines. SD: ~61k of 100,174 rows are on
  multi-item lines. Macro-F1 counts rows, so these two classes are decided mostly on multi-item lines.
* Evidence it is solvable at line level: on all 1,538 SW lines,
  `cash == Σ_ARI(total − round(r·total)) − ΣCN` for exactly one rate (1.25 %: 183, 1.5 %: 542,
  10 %: 813). For SD, `cash == Σ(total − disc on SD rows) + Σ(other rows) − ΣCN` on 4,829 / 4,829.
* Fix: line-level E7/E8; r from the customer table or the entity default; flag rows whose own window is
  valid; require the line identity; otherwise fall back per row.

**BLOCKER 2 — E5 runs before E6, so the 726 instalment pieces that close the item get "" instead of
PARTIAL.** Golden picks the last piece by amount: last = total − (n−1)·⌊total/n⌋. Rows equal to last:
6,329 / 6,329 empty; all other pieces PARTIAL (15,681), including 726 with nothing left open. Fix:
E6 before E5. Same for retention (20 cases where the release is booked first).

Minor disputes: AR duplicate window ≤15 days (50 is the AP figure); keep "not discount-shaped" in E9;
infer instalment n without a PDF only when a repeated piece is visible; exclude open/total < 0.03 from
amount matching.

Points on the table (estimate): exceptions +5 to +6 (SW +1.2, SBC +1.2, OVERPAYMENT +1.0, DUP +0.8,
RETURNED +0.6, SD +0.5, PARTIAL +0.4); partial family +1.5–2; refunds/re-presentation/chargebacks +0.5;
residue gating +0.3–0.5 → **≈ +7 to +9**.

## Treasury — round 2: ACCEPT-WITH-CHANGES
v2 addressed all six round-1 points. Two blockers:

**BLOCKER 1 — the treasury regex list in v2 is missing about half of the patterns.** Literal from TRAIN:
`TRASPASO PROPIO` 1,520 and `TRASPASO ENTRE CUENTAS ES00` 1,576 (CASH_POOL, not INTERNAL);
`TRASPASO DE CTA \d{4}` 968; `FINANCIACION IC` 1,380; `DEVOLUCION PRESTAMO (IC )?VALTERM` 138;
`PRESTAMO INTRAGRUPO` 74; truncated `CHAPS  FINANCIACION IC VA` / `TRANSF. IC VALTERM` 172.
615 of 3,187 intercompany lines sit under generic payment keys where most lines are items (e.g.
`BONIFICO A FAVORE DI CAUSALE FINANCIACION IC …`). IC must fire before LOAN_REPAYMENT. Use the exact
tested set: 19,244 / 19,250 correct, 0 false positives; LIVE 2,377 hits all on known keys.

**BLOCKER 2 — the time-boxed plan never says where party detection comes from.** Third-party markers
(agency, confirming, POR CTA DE) must replace the counterparty (~3,087 LIVE lines); either port the old
detection or implement precedence rule 1 with the factor work.

Not blockers: never inner-join lines to bank_accounts (IT01-VALD-EUR-2 has 1,689 LIVE lines and is
missing; ZA01 has 2); keep loading every entity folder for payment runs.

Points: treasury categories ~+0.2; payment factory +0.3–0.5; factor/confirming/agency +0.3–0.5;
parent / new IBAN / trade name +0.1–0.2 → **≈ +0.9 to +1.4**. The rest of the gap is AR reference and
party resolution and the partial / n-to-m / credit-note families.

## Red-team — round 1 position carried into round 2
REJECT as a from-scratch rewrite for the same day (time), ACCEPT as a design and backlog. Its round-1
blockers (time, wall-clock deadline, two-pass claims) were all accepted by the architect in v2. The
time decision was escalated to the project owner, who chose the **full rewrite**, with the old engine
kept as the shipping fallback until the new one beat it.

## Leaderboard context given to the panel
The leaderboard top was 95 while the old engine stood at 76.90–78.65 on the sample: an 18-point gap
that incremental patches were judged unlikely to close, which supported the rewrite.

## Outcome
Architect v3 was not written as a document (the agent stopped on a billing error); its content was
consolidated directly into `docs/recon2/DESIGN.md` (design v3) and `docs/recon2/API.md`, incorporating
every round-2 fix above, and implemented by six parallel builders.
