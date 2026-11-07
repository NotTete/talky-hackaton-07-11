from bankrec.config import load_settings
from bankrec.data import Phase
from bankrec.alt import advice as A
import collections, pathlib
ph=Phase(load_settings(),"sample")
advs=A.load(ph)
print("advices parsed:", len(advs))
print(collections.Counter(pathlib.Path(a.path).suffix for a in advs))
bad=[a for a in advs if a.total_cents and abs(a.total_cents-a.sum_paid)>max(100,a.total_cents//200)]
print("total mismatch:", len(bad), "/", len(advs))
for a in bad[:4]: print("  BAD", a.path, a.total_cents, a.sum_paid, len(a.lines))
ded=[l for a in advs for l in a.lines if l.deduction_cents]
print("deduction lines:", len(ded), "with reason:", sum(1 for l in ded if l.reason))
print("total lines:", sum(len(a.lines) for a in advs))
for suf in (".pdf",".csv",".txt",".xlsx"):
    for a in advs:
        if a.path.endswith(suf):
            print(suf, a.path, "ref=",a.payment_ref, a.total_cents, a.sum_paid, len(a.lines)); print("   ", a.lines[:2]); break
