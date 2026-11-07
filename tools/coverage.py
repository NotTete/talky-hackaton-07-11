"""How many golden item rows are reachable from each evidence source?"""
import polars as pl, pathlib, re, collections
base=pathlib.Path("data/sample")
R=dict(infer_schema_length=0, null_values=[""])
g=pl.read_csv(base/"golden_allocations.csv.gz", **R)
t=pl.read_csv(base/"bank_transactions.csv.gz", **R)

# item -> document_number
doc={}
ar=pl.read_csv(base/"ar_invoices.csv.gz",**R); ap=pl.read_csv(base/"ap_invoices.csv.gz",**R); cn=pl.read_csv(base/"credit_notes.csv.gz",**R)
for r in ar.iter_rows(named=True): doc[r["item_id"]]=r["document_number"]
for r in ap.iter_rows(named=True): doc[r["item_id"]]=r["supplier_invoice_number"]
for r in cn.iter_rows(named=True): doc[r["item_id"]]=r["document_number"]

def norm(s): return re.sub(r"[^A-Z0-9]","",str(s or "").upper())

# aggregate membership: doc-number sets keyed by every join key
def read_dir(name):
    d=base/name
    if not d.is_dir(): return pl.DataFrame()
    fs=sorted(d.glob("*.csv"))
    return pl.concat([pl.read_csv(f,**R) for f in fs], how="diagonal") if fs else pl.DataFrame()

pr=read_dir("payment_runs"); dd=read_dir("direct_debit_batches"); cs=read_dir("card_settlements"); ch=read_dir("cheque_deposits")

# union of all reference strings available per bank line
TXT=collections.defaultdict(str)
for r in t.iter_rows(named=True):
    TXT[r["bank_txn_id"]]=" ".join(str(r[k] or "") for k in ("text","ref1","ref2","remittance_info","end_to_end_id","counterparty_name"))

# ---- source 1: doc number literally present in the line text (normalised substring)
hit_text=0; tot=0; by_seg=collections.Counter(); seg_tot=collections.Counter()
for r in g.iter_rows(named=True):
    if not r["item_id"]: continue
    tot+=1; seg_tot[r["segment"]]+=1
    dn=norm(doc.get(r["item_id"],""))
    blob=norm(TXT[r["bank_txn_id"]])
    if dn and dn in blob:
        hit_text+=1; by_seg[r["segment"]]+=1
print(f"golden item rows: {tot}")
print(f"  doc-number appears verbatim in bank text: {hit_text} ({hit_text/tot:.1%})")

# ---- source 2: doc number present in an aggregate the line can be joined to
agg_docs=collections.defaultdict(set)   # join-key -> set(normalised doc numbers)
for r in pr.iter_rows(named=True):
    for k in (r.get("end_to_end_id"), r.get("run_id")):
        if k: agg_docs[norm(k)].add(norm(r.get("document_number")))
for r in dd.iter_rows(named=True):
    for k in (r.get("batch_id"), r.get("end_to_end_id"), r.get("mandate_id")):
        if k: agg_docs[norm(k)].add(norm(r.get("remittance_info")))
for r in ch.iter_rows(named=True):
    for k in (r.get("deposit_id"), r.get("cheque_number")):
        if k: agg_docs[norm(k)].add(norm(r.get("stub_reference")))
for r in cs.iter_rows(named=True):
    for k in (r.get("settlement_id"), r.get("merchant_id"), r.get("order_number")):
        if k: agg_docs[norm(k)].add(norm(r.get("order_number")))

hit_agg=0; agg_seg=collections.Counter()
for r in g.iter_rows(named=True):
    if not r["item_id"]: continue
    dn=norm(doc.get(r["item_id"],""))
    if not dn: continue
    blob=norm(TXT[r["bank_txn_id"]])
    found=False
    for key,docs in agg_docs.items():
        if len(key)>=8 and key in blob and any(dn and dn in d or d and dn.endswith(d) or (d and d in dn) for d in docs):
            found=True; break
    if found: hit_agg+=1; agg_seg[r["segment"]]+=1
print(f"  reachable via an aggregate whose key is in the text: {hit_agg} ({hit_agg/tot:.1%})")

print("\nper segment: n_rows / verbatim-ref / via-aggregate")
for s in sorted(seg_tot, key=lambda x:-seg_tot[x]):
    print(f"  {s:30} {seg_tot[s]:>6} {by_seg[s]:>6} {agg_seg[s]:>6}")
