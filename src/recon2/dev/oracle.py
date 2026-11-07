"""Oracle harness for decide(): replay every golden item line of the VERIFIER sample with the
golden item set and true balances, and report per-class exception P/R/F1 + amount accuracy.

    PYTHONPATH=src uv run python -m recon2.dev.oracle [--full] [--learn-wht]

Two columns: `base` (no advice facts) and `adv` (lines carrying a golden SHORT_DISPUTE get the
advice an advice resolver would have produced: per-row paid/ded/reason).
"""

from __future__ import annotations

import argparse
import bisect
import json
import time
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from recon2.decide import Ctx, _disc, decide, split_cash
from recon2.model import Hyp, Item, Line, day

V = Path("/home/tete/.claude/jobs/253f644d/tmp/verifier")
M2 = Path("/home/tete/.claude/jobs/253f644d/tmp/pq/m2.parquet")
ROOT = Path(__file__).resolve().parents[3]
WHT = ROOT / "artifacts" / "wht.json"
CONF_MODE = "base"
CLASSES = ("SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES", "SHORT_DISPUTE", "OVERPAYMENT",
           "PARTIAL", "FX_DIFFERENCE", "DUPLICATE_PAYMENT", "RETURNED")


def _od(v):
    if not v:
        return 0
    return day(v) if isinstance(v, str) else v.toordinal()


# ------------------------------------------------------------- stand-ins
class FXStub:
    def __init__(self):
        f = pl.read_parquet(V / "masters/fx_rates.parquet")
        self.r: dict[str, tuple[list[int], list[Fraction]]] = {}
        for c, d, u in sorted(zip(f["currency"], f["date"], f["units_per_eur"])):
            ds, us = self.r.setdefault(c, ([], []))
            from recon2.model import day
            ds.append(_od(d))
            us.append(Fraction(str(u)))

    def rate(self, ccy, d):
        if ccy == "EUR":
            return Fraction(1)
        ds, us = self.r[ccy]
        return us[max(bisect.bisect_right(ds, d) - 1, 0)]

    def convert(self, cents, src, dst, d):
        q = Fraction(cents) * self.rate(dst, d) / self.rate(src, d)
        return int((q * 2 + 1) // 2)


def masters():
    cu = pl.read_parquet(V / "masters/customers.parquet", columns=["customer_id", "country"])
    en = pl.read_parquet(V / "masters/entities.parquet")
    pt = pl.read_parquet(V / "masters/payment_terms.parquet")
    terms = {c: (int(d or 0), int(round(float(p) * 100)) if p else 0, int(dd or 0))
             for c, d, p, dd in zip(pt["code"], pt["days"], pt["early_payment_discount_pct"], pt["discount_days"])}
    return SimpleNamespace(customers={i: {"country": c} for i, c in zip(cu["customer_id"], cu["country"])},
                           entity_country=dict(zip(en["entity"], en["country"])), terms=terms, fx=FXStub())


class BookStub:
    def __init__(self):
        self.items: list[Item] = []
        self.hist: dict[int, list] = {}


# ------------------------------------------------------------- A3 withholding table
def learn_wht(out: Path = WHT, until: str = "2024-12-31") -> dict[str, int]:
    from datetime import date
    m = (pl.scan_parquet(M2)
         .filter((pl.col("booking_date") <= date.fromisoformat(until)) & (pl.col("kind") == "ARI")
                 & pl.col("i_entity").is_in(["BR01", "MA01", "MX01", "ES01"]) & (pl.col("currency") == pl.col("i_cur")))
         .select("party_id", "ex", "n_items", "amt_c", "tot_c", "k").collect())
    sw = m.filter((pl.col("ex") == "SHORT_WITHHOLDING") & (pl.col("n_items") == 1))
    rates: dict[str, Counter] = defaultdict(Counter)
    for p, a, t in zip(sw["party_id"], sw["amt_c"], sw["tot_c"]):
        s = t - abs(a)
        for bp in (125, 150, 1000):
            if abs(s - (2 * t * bp + 10000) // 20000) <= 1:
                rates[p][bp] += 1
    table = {p: c.most_common(1)[0][0] for p, c in rates.items()}
    anysw = set(m.filter(pl.col("ex") == "SHORT_WITHHOLDING")["party_id"])
    full = (m.filter((pl.col("ex") == "none") & (pl.col("n_items") == 1) & (pl.col("k") == 0)
                     & (pl.col("amt_c").abs() == pl.col("tot_c")))
            .group_by("party_id").len().filter(pl.col("len") >= 3))
    for p in full["party_id"]:
        if p not in anysw:
            table[p] = 0
    table = dict(sorted(table.items()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, indent=0, sort_keys=True))
    return table


# ------------------------------------------------------------- replay
def _hist_rows(ids: list[str], full: bool) -> pl.DataFrame:
    cache = V / "s" / ("hist_full.parquet" if full else "hist_cache.parquet")
    if cache.exists():
        return pl.read_parquet(cache)
    q = pl.scan_parquet(M2).select("item_id", "bank_txn_id", "booking_date", "g_c", "ex")
    if not full:
        q = q.filter(pl.col("item_id").is_in(ids))
    h = q.collect().sort("item_id", "booking_date", "bank_txn_id")
    h.write_parquet(cache)
    return h


def run(full: bool = False, only: str | None = None):
    t0 = time.time()
    r = pl.read_parquet(M2 if full else V / "s/rows.parquet").with_columns(
        (pl.col("rem_after") + pl.col("eff")).alias("rb"))
    if only:
        r = r.filter(pl.col("bank_txn_id") == only)
    hist = _hist_rows(r["item_id"].unique().to_list(), full)
    H: dict[str, list] = defaultdict(list)
    from recon2.model import day
    for iid, tx, d, g, e in hist.iter_rows():
        H[iid].append((_od(d), tx, g, e))
    ms = masters()
    wht = json.loads(WHT.read_text()) if WHT.exists() else learn_wht()
    ctx = Ctx(masters=ms, wht_rate=wht)

    cols = ["bank_txn_id", "item_id", "kind", "segment", "amt_c", "currency", "orig_c", "orig_currency",
            "booking_date", "i_entity", "party_id", "doc", "issue_date", "due_date", "i_cur", "tot_c",
            "payment_terms", "rb", "g_c", "ex"]
    res = {m: [] for m in ("base", "adv")}  # (gold_ex, pred_ex, amt_ok, rule)
    lines_ok = {m: [0, 0] for m in res}
    fit = Counter()
    for (txn,), g in r.select(cols).group_by(["bank_txn_id"], maintain_order=True):
        rows = g.rows(named=True)
        r0 = rows[0]
        ld = _od(r0["booking_date"])
        line = Line(0, txn, "", "", "", "", ld, ld, r0["amt_c"], r0["currency"],
                    abs(r0["orig_c"]) if r0["orig_c"] else None, r0["orig_currency"] or "",
                    "", "", "", "", "", "", "", "", "")
        book = BookStub()
        for x in rows:
            idx = len(book.items)
            book.items.append(Item(idx, x["item_id"], x["kind"], x["i_entity"], x["party_id"] or "", "",
                                   x["doc"] or "", _od(x["issue_date"]), _od(x["due_date"]), x["i_cur"],
                                   x["tot_c"], max(x["rb"], 0), x["payment_terms"] or ""))
            book.hist[idx] = [(0, gc if e != "RETURNED" else -gc, e if e != "none" else "", d)
                              for d, tx, gc, e in H.get(x["item_id"], ()) if (d, tx) < (ld, txn)]
        inv = [i for i, x in enumerate(rows) if x["kind"] in ("ARI", "API")]
        cn = [i for i, x in enumerate(rows) if x["kind"] in ("ARC", "APC")]
        exs = [x["ex"] for x in rows]
        part = next((i for i in inv if exs[i] in ("PARTIAL", "SHORT_DISPUTE")), None)
        hyp = Hyp("ops.card" if r0["segment"] == "ar_card_settlement" else "oracle", 0,
                  inv if inv else cn, cn if inv else [], is_return="RETURNED" in exs, partial_item=part)
        for mode in res:
            if mode == "adv":
                if "SHORT_DISPUTE" not in exs:
                    res["adv"].extend(cur)
                    lines_ok["adv"][0] += ok_line
                    lines_ok["adv"][1] += 1
                    continue
                adv = {}
                for i in inv:
                    it = book.items[i]
                    if exs[i] == "SHORT_DISPUTE":
                        adv[i] = {"paid": rows[i]["g_c"], "ded": it.total - rows[i]["g_c"], "reason": "PRICE DIFF"}
                    elif exs[i] == "SHORT_DISCOUNT":
                        d = _disc(it, line, ctx, book)
                        adv[i] = {"paid": it.total - d, "ded": d, "reason": ""}
                    elif exs[i] == "SHORT_WITHHOLDING" and it.entity == "BR01":
                        adv[i] = {"paid": it.total - (it.total + 5) // 10, "gross": it.total, "ded": 0, "reason": "RET"}
                    elif exs[i] == "PARTIAL":
                        adv[i] = {"paid": rows[i]["g_c"], "ded": 0, "reason": ""}
                    else:
                        adv[i] = {"paid": it.open, "ded": 0, "reason": ""}
                hyp.advice = adv
            applied = split_cash(line, hyp, book, ctx)
            if mode == "base":
                fit["fit" if applied is not None else "nofit"] += 1
            if applied is None:   # measure decide() anyway on golden amounts
                applied = {i: rows[i]["g_c"] for i in range(len(rows))}
            pred = {a.item: a for a in decide(line, hyp, book, ctx, applied)}
            if only:
                print(mode, line.cash, line.ccy, applied, list(pred.values()))
                print([(it.item_id, it.ccy, it.total, it.open, it.party_id, book.hist[it.idx]) for it in book.items])
            cur, ok_line = [], 1
            for i, x in enumerate(rows):
                a = pred.get(i)
                pe = (a.exc or "none") if a else "MISSING"
                aok = a is not None and abs(a.amount - x["g_c"]) <= max(1, 0.005 * x["g_c"])
                ok_line &= int(aok and pe == x["ex"])
                cur.append((x["ex"], pe, aok, a.rule if a else "-", r0["segment"], txn))
            res[mode].extend(cur)
            lines_ok[mode][0] += ok_line
            lines_ok[mode][1] += 1
        hyp.advice = None

    def prf(rs, c):
        tp = sum(1 for g, p, *_ in rs if g == c and p == c)
        fp = sum(1 for g, p, *_ in rs if g != c and p == c)
        fn = sum(1 for g, p, *_ in rs if g == c and p != c)
        pr, rc = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
        return tp + fn, pr, rc, 2 * pr * rc / max(pr + rc, 1e-9)

    print(f"rows={len(res['base'])} lines={lines_ok['base'][1]} split_cash {dict(fit)}  ({time.time()-t0:.1f}s)")
    print(f"{'class':20s} {'gold':>6s} {'P':>6s} {'R':>6s} {'F1':>6s} | {'advF1':>6s} {'amt%':>6s}")
    f1s = {"base": [], "adv": []}
    for c in CLASSES:
        n, p, rc, f = prf(res["base"], c)
        _, _, _, fa = prf(res["adv"], c)
        if n:
            f1s["base"].append(f)
            f1s["adv"].append(fa)
        am = [ok for g, _, ok, *_ in res["base"] if g == c]
        print(f"{c:20s} {n:6d} {p:6.3f} {rc:6.3f} {f:6.3f} | {fa:6.3f} {sum(am)/max(len(am),1):6.3f}")
    am = [ok for g, _, ok, *_ in res["base"] if g == "none"]
    print(f"{'none':20s} {len(am):6d} amt%={sum(am)/max(len(am),1):.4f}")
    for m in res:
        print(f"[{m}] macroF1={sum(f1s[m])/max(len(f1s[m]),1):.4f} amount_acc={sum(x[2] for x in res[m])/len(res[m]):.4f} "
              f"line_acc={lines_ok[m][0]/lines_ok[m][1]:.4f}")
    conf, ex1 = Counter(), {}
    for g, p, _, rule, seg, tx in res[CONF_MODE]:
        if g != p:
            conf[(g, p, rule, seg)] += 1
            ex1.setdefault((g, p, rule, seg), tx)
    print("top confusions (gold -> pred, rule, segment):")
    for (g, p, rule, seg), n in conf.most_common(15):
        print(f"  {n:5d} {g} -> {p} [{rule}] {seg} e.g. {ex1[(g, p, rule, seg)]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--learn-wht", action="store_true")
    a = ap.parse_args()
    if a.learn_wht or not WHT.exists():
        t = learn_wht()
        print(f"wht.json: {len(t)} customers ({sum(1 for v in t.values() if v)} withholders)")
    run(a.full)


if __name__ == "__main__":
    main()
