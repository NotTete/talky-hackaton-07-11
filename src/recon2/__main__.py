"""python -m recon2 run --phase sample|live|final [--out PATH]
   python -m recon2 gap --phase sample [--trace PATH] [--sub PATH]"""

from __future__ import annotations

import argparse
import hashlib
import gzip
import importlib.util
import sys
import time
from pathlib import Path

from recon2.model import DATA

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "out" / "recon2"


def scorer():
    spec = importlib.util.spec_from_file_location("bankrec_score", ROOT / "vendor" / "bankrec_score.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def categories() -> list[str]:
    import csv
    with open(DATA / "masters" / "categories.csv", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        col = rd.fieldnames[0]
        return [r[col] for r in rd]


def score(phase: str, path: Path) -> dict | None:
    g = DATA / phase / "golden_allocations.csv.gz"
    if not g.exists():
        return None
    from recon2.writer import EXCEPTIONS
    sc = scorer()
    gold = sc.load_golden(str(g), str(DATA / phase / "bank_transactions.csv.gz"), sorted(EXCEPTIONS), categories())
    return sc.score_submission(gold, path.read_bytes(), path.name)


def cmd_run(a) -> int:
    from recon2 import engine, trace, writer
    t0 = time.perf_counter()
    eng = engine.Engine(a.phase)
    decs = eng.run()
    out = Path(a.out) if a.out else OUT / f"{a.phase}.csv.gz"
    t = time.perf_counter()
    st = writer.write(decs, eng.item_ids, out, {})
    trace.write(decs, eng.item_ids, out.with_name(out.name.replace(".csv.gz", "") + ".trace.jsonl.gz"))
    eng.timings["write"] = time.perf_counter() - t
    sha = hashlib.sha256(gzip.decompress(out.read_bytes())).hexdigest()[:16]
    n_item = sum(1 for d in decs if d.allocs)
    print(f"[recon2] {a.phase}: {len(decs)} lines, {n_item} with items, rows={st['rows']} demoted={st['demoted']} sha={sha}")
    print("[recon2] timings " + " ".join(f"{k}={v:.1f}s" for k, v in eng.timings.items()) + f" total={time.perf_counter()-t0:.1f}s")
    print(f"[recon2] subset states={eng.res.states} out={out}")
    print("[recon2] modules " + ", ".join(f"{k}={v}" for k, v in sorted(engine.STATUS.items())))
    if eng.errors:
        for k, v in sorted(eng.errors.items(), key=lambda x: -x[1])[:8]:
            print(f"[recon2] ERR {v}x {k}")
    res = score(a.phase, out)
    if res is not None:
        if not res["ok"]:
            print("[recon2] SCORER REJECTED", res["errors"][:5])
            return 1
        m = res["metrics"]
        print(f"[recon2] SCORE {res['score']} " + " ".join(f"{k}={m[k]:.4f}" for k in ("allocation_f1", "txn_accuracy", "exception_macro_f1") if k in m))
    return 0


def cmd_gap(a) -> int:
    from recon2 import gap
    out = Path(a.sub) if a.sub else OUT / f"{a.phase}.csv.gz"
    tr = Path(a.trace) if a.trace else out.with_name(out.name.replace(".csv.gz", "") + ".trace.jsonl.gz")
    print(gap.report(a.phase, out, tr, top=a.top))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="recon2")
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--phase", default="sample")
    r.add_argument("--out")
    g = sp.add_parser("gap")
    g.add_argument("--phase", default="sample")
    g.add_argument("--sub")
    g.add_argument("--trace")
    g.add_argument("--top", type=int, default=25)
    a = p.parse_args(argv)
    return {"run": cmd_run, "gap": cmd_gap}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
