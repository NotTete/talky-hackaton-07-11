"""trace.jsonl.gz — one record per line, sorted keys (DESIGN §6)."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from recon2.model import Decision

REASONS = {
    "NO_PARTY", "PARTY_AMBIGUOUS", "NO_REF", "REF_NOT_FOUND", "REF_AMBIGUOUS", "OP_NOT_FOUND",
    "OP_FEE_LINE", "ADVICE_NOT_FOUND", "ADVICE_ROW_UNRESOLVED", "AMOUNT_NO_FIT", "FX_OUT_OF_BAND",
    "ITEM_CLOSED", "ITEM_STALE", "RESIDUE_BLOCKED", "AMOUNT_ONLY_FORBIDDEN", "FANOUT_CAP",
    "SUBSET_CAP", "GUARD_DEMOTED", "TREASURY", "ROUTER",
}


def record(d: Decision, item_ids: list[str]) -> dict:
    t = d.trace
    return {
        "txn": d.txn,
        "route": t.get("route", ""),
        "resolver": t.get("resolver", ""),
        "tier": t.get("tier", -1),
        "parties": [list(p) for p in t.get("parties", [])][:10],
        "cands": [list(c) for c in t.get("cands", [])][:10],
        "rows": [[item_ids[a.item], a.amount, a.exc, a.rule] for a in d.allocs],
        "category": d.category,
        "reason": t.get("reason", ""),
        "caps": list(t.get("caps", [])),
    }


def write(decisions: list[Decision], item_ids: list[str], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, filename="") as gz:
        for d in decisions:
            gz.write((json.dumps(record(d, item_ids), sort_keys=True, ensure_ascii=False) + "\n").encode())
    os.replace(tmp, path)


def read(path: Path) -> dict[str, dict]:
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for ln in fh:
            r = json.loads(ln)
            out[r["txn"]] = r
    return out
