# ROUND 2 — DOCS on v2: ACCEPT-WITH-CHANGES (ordering, not design)
1 Advices scheduled last (B6) but biggest doc lever: ar_short_dispute 8,318 golden rows (.507; 23% of sample golden rows), ar_1toN_remittance (.837), ar_n_to_m (417 rows, .098). Linkage solved (18,887/18,889 on payment_date); parsing is the risk. Move money()+advices early; must cover Sep-2025 Voucher/Invoice xlsx (North Arden US01, Gray & Cox UK01), txt refs PAY-…/REM/…, US mm/dd header.
2 t_join (n43 38-char glue) missing from time-box but party step 5 depends on it; ~20 LOC; put first.
3 pdfclauses: don't defer entirely. Minimum now (~40 min): replaces regex (EN+ES, digits, two-dash), instalment amounts with money() fix, rappel/scn direction table. Defer service_month/retention/advance. Old pdf_facts.jsonl must NOT stay wired (62 wrong replaces, EN instalment amounts garbage).
Points estimate toward 95: advices ~4-7 pts; PDF clauses ~1-1.5; t_join + notices ~0.5-1. Total ~6-9 of the ~18 gap.
