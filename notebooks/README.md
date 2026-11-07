# Notebooks

Throwaway exploration lives here (not imported by the pipeline). Useful first cells:

```python
import sys; sys.path.insert(0, "../src")
from bankrec.config import load_settings
from bankrec.data import Phase

phase = Phase(load_settings(), "sample")
txns = phase.bank_transactions()
items = phase.items()
golden = phase.golden()

# what does a bank line look like?
print(txns[["booking_date", "amount", "text"]].head(20).to_string())

# which segments exist and how many rows/line each?
print(golden.groupby("segment").agg(rows=("bank_txn_id", "size"), lines=("bank_txn_id", "nunique")))

# join the labels to the bank line that produced them
merged = golden.merge(txns, on="bank_txn_id")
print(merged[merged.segment == "ar_short_dispute"][["text", "item_id", "amount_x", "amount_y", "exception"]].head())
```

Rule of thumb: pick the segment with the most *rows* you do not cover yet, read 20
examples, and turn the pattern you see into one branch in `matching.py` or `rules.py`.
