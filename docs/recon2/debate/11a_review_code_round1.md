# REVIEWER-CODE findings (summary)
No golden read in resolve/decide; golden only in learners + engine._sample_carry.
HIGH 1 FINAL carry rejoin only to TRAIN tables (load._carried) -> LIVE-issued carried items lose terms/method/po/related in FINAL. Fix: also concat DATA/live (+phase) ar/ap/cn.
HIGH 2 Silent stubs (_try, except blocks, _call swallowing per-line errors) -> LIVE could validate with low recall. Fix: strict mode for live/final; abort if errors > 0.1% lines.
HIGH 3 _sample_carry filters bank_txn_id < first (string) not date: 521 TRAIN lines with id < first but booking >= 2025-06-01 replayed as prior payments (small future leak). Fix: booking_date < window start.
HIGH 4 artifacts/aliases.json until 2099 includes eval periods; use aliases_2024.json for every golden-scored eval; commit it.
MED 5 train:* windows: ops loads all TRAIN op files incl. after window (slow, can inflate). Restrict months.
MED 6 FINAL: masters_final path, ZA01 ZAR fx -> fx.py raises KeyError caught by _call -> silent. Make loud/non-raising.
LOW 8 _carried unique without maintain_order; add PYTHONHASHSEED determinism test.
LOW 9 writer: no assert all phase lines appear; add.
OK 10 decide order matches DESIGN (E6 before E5, line-level E7/E8). "never amount alone" in resolve.py NOT audited.
OK 11 perf budgets step-counted; worst case 200k states x many lines.
