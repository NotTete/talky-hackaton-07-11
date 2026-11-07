"""Reference scorer for the Talky x Anthropic bank-reconciliation hackathon (SPEC §6-7).

Python standard library only (runs in AWS Lambda python3.12, 1 vCPU / 1 GB). Stable API:

    golden = load_golden(golden_csv_gz_path, phase_txns_csv_gz_path, exceptions, categories)
    result = score_submission(golden, data: bytes, filename: str)   # never raises

Submission: CSV (UTF-8, header, comma; ';' is also accepted), optionally gzip (.csv.gz), one row per allocation:
    bank_txn_id,item_id,amount,category,exception

Score = 100 * (0.5 * allocation_F1 + 0.3 * txn_accuracy + 0.2 * exception_macro_F1), 2 decimals.
"""
from __future__ import annotations

import csv
import gzip
import io
import math
import re
import zlib

__all__ = ["Golden", "load_golden", "score_submission", "MAX_UNCOMPRESSED", "MAX_ROWS"]

MAX_UNCOMPRESSED = 60 * 1024 * 1024     # submission limit (uncompressed)
MAX_DECOMPRESS = 80 * 1024 * 1024       # hard guard while inflating (gzip bomb)
MAX_ROWS = 3_000_000
MAX_ERRORS = 50
W_ALLOC, W_TXN, W_EXC = 0.5, 0.3, 0.2
REQUIRED = ["bank_txn_id", "item_id", "amount", "category", "exception"]
ITEM_RE = re.compile(r"^(ARI|ARC|API|APC)-\d{4}-\d{7}$")
AMT_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _tol(g):
    return max(0.01, 0.005 * abs(g)) + 1e-9


class Golden:
    """golden of one phase.
    txn -> (items: dict item_id -> (amount, exception), category, exception_of_noitem_line, segment)"""

    def __init__(self):
        self.txns = {}
        self.weight = {}
        self.exceptions = []
        self.categories = []
        self.n_pairs = 0

    def __len__(self):
        return len(self.txns)


def _open_text(path):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8-sig", newline="")
    return open(path, encoding="utf-8-sig", newline="")


def load_golden(golden_csv_gz_path: str, phase_txns_csv_gz_path: str, exceptions: list, categories: list) -> Golden:
    g = Golden()
    g.exceptions = [e for e in exceptions if e]
    g.categories = list(categories)
    with _open_text(phase_txns_csv_gz_path) as fh:
        rd = csv.reader(fh)
        header = next(rd)
        ia = header.index("amount_eur") if "amount_eur" in header else None
        for row in rd:
            if not row:
                continue
            t = row[0].strip()
            g.txns[t] = [{}, "", "", ""]
            if ia is not None and len(row) > ia and row[ia]:
                g.weight[t] = abs(float(row[ia]))
    with _open_text(golden_csv_gz_path) as fh:
        rd = csv.reader(fh)
        h = next(rd)
        ix = {k: h.index(k) for k in h}
        for row in rd:
            if not row:
                continue
            t = row[ix["bank_txn_id"]]
            rec = g.txns.get(t)
            if rec is None:
                rec = g.txns[t] = [{}, "", "", ""]
            item = row[ix["item_id"]]
            exc = row[ix["exception"]]
            if "segment" in ix:
                rec[3] = row[ix["segment"]]
            if item:
                rec[0][item] = (float(row[ix["amount"]]), exc)
                g.n_pairs += 1
            else:
                rec[1] = row[ix["category"]]
                rec[2] = exc
    for t, rec in g.txns.items():
        g.txns[t] = (rec[0], rec[1], rec[2], rec[3] or "unlabelled")
        if t not in g.weight:
            g.weight[t] = sum(a for a, _ in rec[0].values()) or 1.0
    return g


def _inflate(data: bytes):
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = []
    size = 0
    buf = data
    while buf:
        chunk = d.decompress(buf, 1024 * 1024)
        size += len(chunk)
        if size > MAX_DECOMPRESS:
            raise ValueError("decompressed file exceeds 80 MB (limit is 60 MB uncompressed)")
        out.append(chunk)
        buf = d.unconsumed_tail
        if d.eof:
            if d.unused_data.strip(b"\x00"):
                # concatenated gzip members: keep inflating
                buf = d.unused_data
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            else:
                break
    if not d.eof and not buf:
        tail = d.flush()
        size += len(tail)
        out.append(tail)
    return b"".join(out)


def _result(ok, errors, score=None, metrics=None, segments=None):
    return {"ok": ok, "errors": errors[:MAX_ERRORS], "score": score, "metrics": metrics or {}, "segments": segments or {}}


def _err(errors, row, msg):
    if len(errors) < MAX_ERRORS:
        errors.append({"row": row, "message": msg})


def score_submission(golden: Golden, data: bytes, filename: str) -> dict:
    try:
        return _score(golden, data, filename or "")
    except Exception as ex:   # never raise to the caller
        return _result(False, [{"row": None, "message": f"could not process the file ({type(ex).__name__})"}])


def _score(golden, data, filename):
    errors = []
    if data is None or len(data) == 0:
        return _result(False, [{"row": None, "message": "empty file"}])
    fn = filename.lower()
    is_gz = data[:2] == b"\x1f\x8b"
    if fn.endswith(".gz") and not is_gz:
        return _result(False, [{"row": None, "message": "file name ends with .gz but the content is not gzip"}])
    if is_gz:
        try:
            data = _inflate(data)
        except ValueError as ex:
            return _result(False, [{"row": None, "message": str(ex)}])
        except Exception:
            return _result(False, [{"row": None, "message": "invalid gzip file"}])
    elif not (fn.endswith(".csv") or fn.endswith(".txt") or fn == ""):
        return _result(False, [{"row": None, "message": "unsupported file type: upload .csv or .csv.gz"}])
    if len(data) > MAX_UNCOMPRESSED:
        return _result(False, [{"row": None, "message": f"file too large: {len(data) / 1e6:.1f} MB uncompressed (limit 60 MB)"}])
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return _result(False, [{"row": None, "message": "file is not valid UTF-8"}])
    if b"\x00" in data[:4096]:
        return _result(False, [{"row": None, "message": "file looks binary (NUL bytes); upload a UTF-8 CSV"}])
    stream = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="")
    first = stream.readline()
    delim = ","
    if first.count(";") >= 4 and first.count(",") < 4:
        delim = ";"
    header = [h.strip().lower() for h in next(csv.reader([first], delimiter=delim), [])]
    if not header or header == [""]:
        return _result(False, [{"row": None, "message": "empty file"}])
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        return _result(False, [{"row": 1, "message": f"missing column(s): {', '.join(missing)}; expected header: {','.join(REQUIRED)}"}])
    extra = [c for c in header if c not in REQUIRED and c]
    if extra:
        return _result(False, [{"row": 1, "message": f"unexpected column(s): {', '.join(extra)}; expected header: {','.join(REQUIRED)}"}])
    ix_t, ix_i, ix_a, ix_c, ix_e = (header.index(c) for c in REQUIRED)
    ncol = len(header)
    exc_ok = set(golden.exceptions) | {""}
    cat_ok = set(golden.categories) | {""}
    txns = golden.txns
    # streaming accumulation (low memory): per txn [n_item_rows, n_ok_rows, has_noitem, noitem_cat, noitem_exc]
    state = {}
    seen = set()
    exc_tp = {c: 0 for c in golden.exceptions}
    exc_fp = {c: 0 for c in golden.exceptions}
    tp = 0
    n_sub_pairs = 0
    n_rows = 0
    rd = csv.reader(stream, delimiter=delim)
    for lineno, row in enumerate(rd, start=2):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        n_rows += 1
        if n_rows > MAX_ROWS:
            return _result(False, [{"row": lineno, "message": f"too many rows (limit {MAX_ROWS:,})"}])
        if len(row) != ncol:
            _err(errors, lineno, f"expected {ncol} fields, found {len(row)}")
            continue
        t = row[ix_t].strip()
        item = row[ix_i].strip()
        amt_s = row[ix_a].strip()
        cat = row[ix_c].strip().upper()
        exc = row[ix_e].strip().upper()
        if not t:
            _err(errors, lineno, "bank_txn_id is empty")
            continue
        g = txns.get(t)
        if g is None:
            _err(errors, lineno, f"bank_txn_id {t} is not a bank transaction of this phase")
            continue
        if exc not in exc_ok:
            _err(errors, lineno, f"unknown exception '{exc}' (allowed: empty or {', '.join(golden.exceptions)})")
            continue
        if cat not in cat_ok:
            _err(errors, lineno, f"unknown category '{cat}' (see categories.csv)")
            continue
        st = state.get(t)
        if st is None:
            st = state[t] = [0, 0, False, "", ""]
        if item:
            if not ITEM_RE.match(item):
                _err(errors, lineno, f"item_id '{item}' is not a valid id (e.g. ARI-2025-0123456)")
                continue
            if cat:
                _err(errors, lineno, "category must be empty when item_id is given")
                continue
            if delim == ";":
                amt_s = amt_s.replace(",", ".")
            if not AMT_RE.match(amt_s):
                _err(errors, lineno, f"invalid amount '{amt_s}' (use a dot as decimal separator, e.g. 1210.00)")
                continue
            amt = float(amt_s)
            if not math.isfinite(amt) or amt < 0 or amt > 1e12:
                _err(errors, lineno, f"invalid amount '{amt_s}' (must be a non-negative number in the item currency)")
                continue
            key = t + "|" + item
            if key in seen:
                _err(errors, lineno, f"duplicate row for ({t}, {item})")
                continue
            if st[2]:
                _err(errors, lineno, f"{t} has both item rows and a no-item (category) row")
                continue
            seen.add(key)
            st[0] += 1
            n_sub_pairs += 1
            gi = g[0].get(item)
            ge = gi[1] if gi is not None else None
            if gi is not None and abs(amt - gi[0]) <= _tol(gi[0]):
                tp += 1
                if exc == ge:
                    st[1] += 1
            if exc:
                if exc == ge:
                    exc_tp[exc] += 1
                else:
                    exc_fp[exc] += 1
        else:
            if amt_s:
                _err(errors, lineno, "amount must be empty when item_id is empty")
                continue
            if st[2]:
                _err(errors, lineno, f"duplicate no-item row for {t}")
                continue
            if st[0]:
                _err(errors, lineno, f"{t} has both item rows and a no-item (category) row")
                continue
            st[2], st[3], st[4] = True, cat, exc
            if exc:
                if not g[0] and exc == g[2]:
                    exc_tp[exc] += 1
                else:
                    exc_fp[exc] += 1
    if errors:
        return _result(False, errors)
    if n_rows == 0:
        return _result(False, [{"row": None, "message": "no data rows"}])
    # ---------------------------------------------------------------- metrics
    del seen
    correct_txn = 0
    w_tot = 0.0
    w_ok = 0.0
    seg_n = {}
    seg_ok = {}
    gold_cnt = {c: 0 for c in golden.exceptions}
    weight = golden.weight
    for t, (gitems, gcat, gexc, seg) in txns.items():
        st = state.get(t)
        if gitems:
            for ga, ge in gitems.values():
                if ge in gold_cnt:
                    gold_cnt[ge] += 1
            ok = st is not None and not st[2] and st[0] == len(gitems) and st[1] == len(gitems)
        else:
            if gexc in gold_cnt:
                gold_cnt[gexc] += 1
            ok = st is not None and st[2] and st[0] == 0 and st[3] == gcat and st[4] == gexc
        w = weight.get(t, 1.0)
        w_tot += w
        seg_n[seg] = seg_n.get(seg, 0) + 1
        if ok:
            correct_txn += 1
            w_ok += w
            seg_ok[seg] = seg_ok.get(seg, 0) + 1
    gp = golden.n_pairs
    precision = tp / n_sub_pairs if n_sub_pairs else 0.0
    recall = tp / gp if gp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    acc = correct_txn / len(txns) if txns else 0.0
    f1s = []
    for c in golden.exceptions:
        if not gold_cnt[c]:
            continue
        fn = gold_cnt[c] - exc_tp[c]
        p_ = exc_tp[c] / (exc_tp[c] + exc_fp[c]) if exc_tp[c] + exc_fp[c] else 0.0
        r_ = exc_tp[c] / (exc_tp[c] + fn) if exc_tp[c] + fn else 0.0
        f1s.append(2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0.0)
    macro = sum(f1s) / len(f1s) if f1s else 1.0
    score = round(100 * (W_ALLOC * f1 + W_TXN * acc + W_EXC * macro), 2)
    metrics = {
        "allocation_f1": round(f1, 6), "precision": round(precision, 6), "recall": round(recall, 6),
        "txn_accuracy": round(acc, 6), "exception_macro_f1": round(macro, 6),
        "amount_weighted_accuracy": round(w_ok / w_tot, 6) if w_tot else 0.0,
        "rows": n_rows, "txns_covered": len(state), "txns_total": len(txns),
    }
    segments = {k: {"n": n, "accuracy": round(seg_ok.get(k, 0) / n, 6)} for k, n in sorted(seg_n.items())}
    return _result(True, [], score, metrics, segments)


if __name__ == "__main__":
    import json
    import sys
    import time
    if len(sys.argv) < 4:
        print("usage: bankrec_score.py GOLDEN.csv.gz PHASE_TXNS.csv.gz SUBMISSION.csv[.gz] [exceptions.json categories.json]")
        sys.exit(2)
    exc = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else ["PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES",
                                                                  "SHORT_WITHHOLDING", "SHORT_DISPUTE", "DUPLICATE_PAYMENT", "UNIDENTIFIED",
                                                                  "RETURNED", "FX_DIFFERENCE"]
    cats = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else []
    t0 = time.time()
    g = load_golden(sys.argv[1], sys.argv[2], exc, cats)
    t1 = time.time()
    res = score_submission(g, open(sys.argv[3], "rb").read(), sys.argv[3])
    t2 = time.time()
    res["timing"] = {"load_golden_s": round(t1 - t0, 3), "score_s": round(t2 - t1, 3)}
    print(json.dumps(res, indent=1))
