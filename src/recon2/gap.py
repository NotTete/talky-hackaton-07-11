"""Gap report: trace x golden -> miss buckets ranked by points lost (DESIGN §6).

Points per line are a marginal approximation of the score formula:
  txn accuracy: 30/N_lines for a wrong line;
  allocation F1: 50*F1/(2TP+FP+FN) per missing or extra pair;
  exception macro-F1 is not apportioned (reported per class at the bottom).
"""

from __future__ import annotations

import csv
import gzip
from collections import defaultdict
from pathlib import Path

from recon2 import trace as T
from recon2.model import DATA


def _tol(g: float) -> float:
    return max(0.01, 0.005 * abs(g)) + 1e-9


def _read(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rec = out.setdefault(r["bank_txn_id"], {"items": {}, "cat": "", "exc": "", "seg": ""})
            if r.get("segment"):
                rec["seg"] = r["segment"]
            if r["item_id"]:
                rec["items"][r["item_id"]] = (float(r["amount"]), r["exception"])
            else:
                rec["cat"], rec["exc"] = r["category"], r["exception"]
    return out


def classify(g: dict, s: dict, tr: dict) -> tuple[str, int, int]:
    """(bucket, fn, fp) for one line; bucket '' if the line is correct."""
    gi, si = g["items"], s["items"]
    tp = sum(1 for k, (a, _) in si.items() if k in gi and abs(a - gi[k][0]) <= _tol(gi[k][0]))
    fn, fp = len(gi) - tp, len(si) - tp
    res = tr.get("resolver") or tr.get("route") or "?"
    rules = {r[0]: r[3] for r in tr.get("rows", [])}
    if not gi:
        if si:
            return f"EXTRA_FROM({res})", fn, fp
        if s["cat"] != g["cat"] or s["exc"] != g["exc"]:
            return f"WRONG_CAT({g['cat']}<-{s['cat']})", 0, 0
        return "", 0, 0
    if not si:
        cands = {c[0] for c in tr.get("cands", [])}
        why = tr.get("reason") or "?"
        caps = [c.split(":")[0] for c in tr.get("caps", [])]
        if caps:
            why += "/" + caps[0]
        if s["cat"] and tr.get("route") in ("treasury", "refund"):
            return f"ITEM_EXPECTED({tr.get('route')}:{s['cat']})", fn, fp
        if cands & set(gi):
            return f"REJECTED({why})", fn, fp
        return f"NOT_GENERATED({why})", fn, fp
    if not set(si) & set(gi):
        return f"CHOSEN_OTHER({res})", fn, fp
    if set(si) != set(gi):
        return (f"MISSING_ITEMS({res})" if set(si) < set(gi) else f"EXTRA_ITEMS({res})"), fn, fp
    for k, (a, e) in si.items():
        ga, ge = gi[k]
        if abs(a - ga) > _tol(ga):
            return f"AMOUNT_OFF({rules.get(k, '?')}:{ge or '-'}<-{e or '-'})", fn, fp
    for k, (a, e) in si.items():
        if e != gi[k][1]:
            return f"EXC_WRONG({rules.get(k, '?')}:{gi[k][1] or '-'}<-{e or '-'})", fn, fp
    return "", fn, fp


def report(phase: str, sub: Path, trace_path: Path, top: int = 25) -> str:
    from recon2.__main__ import phase_files
    gold = _read(phase_files(phase)[0])
    subm = _read(sub)
    trs = T.read(trace_path) if Path(trace_path).exists() else {}
    n = len(gold)
    tp_all = fp_all = fn_all = 0
    rows = []
    for txn in sorted(gold):
        g = gold[txn]
        s = subm.get(txn, {"items": {}, "cat": "", "exc": ""})
        b, fn, fp = classify(g, s, trs.get(txn, {}))
        tp_all += len(g["items"]) - fn
        fp_all += fp
        fn_all += fn
        if b:
            rows.append((txn, g, s, b, fn, fp))
    denom = 2 * tp_all + fp_all + fn_all or 1
    f1 = 2 * tp_all / denom
    pair_pts = 50 * f1 / denom
    line_pts = 30 / n
    buckets: dict[str, list] = defaultdict(list)
    segs: dict[str, float] = defaultdict(float)
    segn: dict[str, int] = defaultdict(int)
    for txn, g, s, b, fn, fp in rows:
        pts = line_pts + pair_pts * (fn + fp)
        buckets[b].append((pts, txn, g, s))
        segs[g["seg"]] += pts
        segn[g["seg"]] += 1
    lost = sum(p for v in buckets.values() for p, *_ in v)
    out = [f"# gap {phase}: {len(rows)}/{n} lines wrong, ~{lost:.2f} pts lost (excl. exception macro-F1); "
           f"F1={f1:.4f} TP={tp_all} FP={fp_all} FN={fn_all}", "", "## buckets", "",
           "| bucket | lines | pts |", "|---|---|---|"]
    ranked = sorted(buckets.items(), key=lambda kv: -sum(p for p, *_ in kv[1]))
    for b, v in ranked[:top]:
        out.append(f"| {b} | {len(v)} | {sum(p for p, *_ in v):.2f} |")
    out += ["", "## segments", "", "| segment | wrong lines | pts |", "|---|---|---|"]
    for sname, p in sorted(segs.items(), key=lambda kv: -kv[1])[:top]:
        out.append(f"| {sname} | {segn[sname]} | {p:.2f} |")
    out += ["", "## examples"]
    for b, v in ranked[:top]:
        out.append(f"\n### {b}")
        for pts, txn, g, s in sorted(v, key=lambda x: x[1])[:3]:
            gi = ";".join(f"{k}:{a:.2f}:{e}" for k, (a, e) in sorted(g["items"].items())[:4]) or f"cat={g['cat']}"
            si = ";".join(f"{k}:{a:.2f}:{e}" for k, (a, e) in sorted(s["items"].items())[:4]) or f"cat={s['cat']}"
            out.append(f"- {txn} [{g['seg']}] gold {gi} | sub {si}")
    # exception per-class view
    cls = defaultdict(lambda: [0, 0, 0])
    for txn, g in gold.items():
        s = subm.get(txn, {"items": {}, "cat": "", "exc": ""})
        for k, (_, e) in g["items"].items():
            if e:
                cls[e][1] += 1
                if s["items"].get(k, (0, ""))[1] == e:
                    cls[e][0] += 1
        for k, (_, e) in s["items"].items():
            if e:
                cls[e][2] += 1
        if not g["items"] and g["exc"]:
            cls[g["exc"]][1] += 1
            if not s["items"] and s["exc"] == g["exc"]:
                cls[g["exc"]][0] += 1
        if not s["items"] and s["exc"]:
            cls[s["exc"]][2] += 1
    out += ["", "## exception classes (tp/gold/sub -> F1)", ""]
    for e, (tp, gn, sn) in sorted(cls.items()):
        f = 2 * tp / (gn + sn) if gn + sn else 0
        out.append(f"- {e}: {tp}/{gn}/{sn} F1={f:.3f}")
    return "\n".join(out)
