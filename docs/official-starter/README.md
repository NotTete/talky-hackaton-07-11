# starter

Standard library only (Python ≥ 3.10). Run from the folder where you extracted the archives (it contains `masters/`,
`train/`, `live/`…):

```
python starter/starter.py --phase sample --out sub_sample.csv.gz     # 1 month, seconds
python starter/score_train.py sub_sample.csv.gz --sample

python starter/starter.py --phase train --out sub_train.csv.gz       # 4 years (~1.5 M lines, a few minutes, ~8 GB RAM)
python starter/score_train.py sub_train.csv.gz

python starter/starter.py --phase live --out sub_live.csv.gz         # upload this one to the leaderboard
```

`starter.py` matches a bank line to an item only when the bank text contains one of our printed document numbers and the
amount is exactly the document total; everything else gets a placeholder row (receipts → `UNIDENTIFIED`, payments →
`BANK_FEES`). It scores ~10–15 (the reference naive baseline with an exact-amount fallback scores ~19 on LIVE): the rest is up to you. `bankrec_score.py` is the exact scorer used by the server.
