"""Trace one bank line through the alt engine: which resolver fires, and why."""
import sys, collections, pathlib, polars as pl
from bankrec.config import load_settings
from bankrec.data import Phase
from bankrec.alt.engine import Engine, _Context
from bankrec.alt import refs
R=dict(infer_schema_length=0,null_values=[""])
phase=sys.argv[2] if len(sys.argv)>2 else "sample"
ph=Phase(load_settings(),phase)
eng=Engine(ph, ph.items(), ph.masters)
txns=ph.bank_transactions()
want=set(sys.argv[1].split(","))
base=pathlib.Path("data")/phase
g=pl.read_csv(base/"golden_allocations.csv.gz",**R) if (base/"golden_allocations.csv.gz").exists() else None
gold=collections.defaultdict(list)
if g is not None:
    for r in g.iter_rows(named=True): gold[r["bank_txn_id"]].append(r)
doc={}
for f,c in (("ar_invoices.csv.gz","document_number"),("ap_invoices.csv.gz","supplier_invoice_number"),("credit_notes.csv.gz","document_number")):
    pp=base/f
    if pp.exists():
        for r in pl.read_csv(pp,**R).iter_rows(named=True): doc[r["item_id"]]=r[c]
for txn in txns.iter_rows(named=True):
    bid=txn["bank_txn_id"]
    if bid not in want:
        eng.resolve(txn)   # keep the ledger state honest
        continue
    ctx=_Context(txn,eng)
    print("="*100)
    print(bid, txn["amount"], txn["currency"], "| party:", ctx.party, eng.ledger.masters.party_names.get(ctx.party,""))
    print("  text:", (txn.get("text") or "")[:160])
    print("  tokens:", ctx.tokens[:14])
    print("  op_tokens:", ctx.op_tokens[:10], "want_count:", ctx.want_count)
    op=eng.ops.by_token(ctx.op_tokens)
    print("  op by token:", (op.kind, op.total_cents, len(op.entries)) if op else None)
    adv=eng.advices.by_token(ctx.op_tokens)
    print("  advice by ref:", adv.path if adv else None)
    if ctx.party:
        adv2=eng.advices.by_party_total(ctx.party, abs(ctx.cash), ctx.ymd, max(2,abs(ctx.cash)//100))
        print("  advice by party+total:", adv2.path if adv2 else None,
              " (party has", len(eng.advices._by_party.get(ctx.party,[])), "advices)")
    for t in ctx.tokens[:12]:
        hit=eng.ledger.by_doc(t)
        if hit: print(f"    ref {t} -> {hit[:3]}")
    rows=eng.resolve(txn)
    print("  RESULT:", [(r.item_id or r.category, r.cents, r.exception) for r in rows][:10], f"({len(rows)} rows)")
    if gold.get(bid):
        print("  GOLDEN:", [(x['item_id'] or x['category'], x['amount'], x['exception'], doc.get(x['item_id'],'')) for x in gold[bid]][:10], f"({len(gold[bid])} rows)")
