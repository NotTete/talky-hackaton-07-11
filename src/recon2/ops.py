"""Ops files (T0): DD batches, card settlements, cheque deposits, payment runs.

The files on disk contain the answer; the only questions are (1) which operation a bank
line names and (2) whether the line's amount is explained by it (a fee line reuses the
batch id: "COMISION REMESA 03250603661 125 RECIBOS").  resolve() answers both and maps
file rows to items.  Per-row allocation amounts travel in hyp.advice[idx]['paid'].
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from .model import Hyp, Line, day

FACTORS = ("CONFIRMING PENALARA", "CREDIFACIL COMERCIAL EFC", "RIBAGORZA CONFIRMING",
           "NORTE PAGOS CONFIRMING EFC")
AP_POOL = ("ES00", "ES01", "ES02", "PT01")
RUN_LOOKBACK = 4      # months of previous-phase payment runs (late confirming debits)
OTHER_LOOKBACK = 2    # months of previous-phase DD / card / cheque files (returns, late cards)
MAX_STATES = 30_000
MAX_GROUPS = 80

_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/\-]{4,}")
_COUNT = re.compile(r"\b(\d{1,4}) (?:RECIBOS|PROVEEDORES|OPERATIONS|POSTEN|CHQ|OP|TRX|ITEMS|TRANSFERS)\b")
_RUN_REF = re.compile(r"\b(PR\d{10,14})-0?\b")
_SEQ6 = re.compile(r"\b(\d{6})\s*$")
_MID = re.compile(r"(?<!\d)(\d{9})(?!\d)")


def fold(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ./\-+*]", " ", s)).strip()


def cents(s) -> int:
    if s is None or s == "":
        return 0
    s = str(s).strip()
    neg = s.startswith("-")
    s = s.lstrip("-+")
    whole, _, frac = s.partition(".")
    v = int(whole or 0) * 100 + int((frac + "00")[:2])
    return -v if neg else v


def line_text(line: Line) -> str:
    if line.t_join:
        return line.t_join
    return fold(" | ".join(x for x in (line.text, line.ref1, line.ref2, line.rinfo,
                                      line.cpty_name, line.e2e) if x))


def tokens_of(text: str) -> list[str]:
    out = set()
    for t in _TOKEN.findall(text):
        t = t.strip("-/")
        out.add(t)
        if "-" in t:
            out.update(p for p in t.split("-") if len(p) >= 6)
    return sorted(out)


@dataclass(slots=True)
class Row:
    doc: str          # document reference to look up (remittance_info / order / stub / doc number)
    amt: int          # signed cents (runs: negative = credit note)
    party: dict       # hints: mandate, iban, name, card
    group: str        # e2e (runs) / own index (others)
    gross: int = 0
    net: int = 0


@dataclass(slots=True)
class Op:
    kind: str         # dd | card | cheque | run
    oid: str
    account: str
    entity: str
    day: int
    total: int        # cash the bank line should show (abs)
    rows: list[Row] = field(default_factory=list)
    groups: dict[str, int] = field(default_factory=dict)   # e2e -> transfer amount (runs)
    mid: str = ""
    ccy: str = ""


def _ym(name: str) -> int:
    m = re.search(r"_(\d{6})(?:_\d+)?\.csv", name)
    return int(m.group(1)) if m else 0


def _mback(ym: int, k: int) -> int:
    y, m = divmod(ym, 100)
    m -= k
    while m <= 0:
        m += 12
        y -= 1
    return y * 100 + m


class Ops:
    def __init__(self, phase, extra_roots: list[Path] | None = None):
        self.phase = phase
        self.ops: dict[str, Op] = {}
        self.by_key: dict[str, list[tuple[Op, str | None]]] = defaultdict(list)
        self.cards_by_mid: dict[str, list[Op]] = defaultdict(list)
        self.dd_by_mandate: dict[str, list[tuple[Op, int]]] = defaultdict(list)
        self.chq_by_number: dict[str, list[tuple[Op, int]]] = defaultdict(list)
        root = Path(phase.root)
        months = [_ym(p.name) for d in ("direct_debit_batches", "card_settlements",
                                        "cheque_deposits", "payment_runs")
                  for p in (root / d).glob("*.csv")]
        start = min((m for m in months if m), default=0)
        for r in [root, *[Path(x) for x in (extra_roots or [])]]:
            extra = r != root
            for kind, sub, lb in (("dd", "direct_debit_batches", OTHER_LOOKBACK),
                                  ("card", "card_settlements", OTHER_LOOKBACK),
                                  ("cheque", "cheque_deposits", OTHER_LOOKBACK),
                                  ("run", "payment_runs", RUN_LOOKBACK)):
                for p in sorted((r / sub).glob("*.csv")):
                    if extra and start and not (_mback(start, lb) <= _ym(p.name) < start):
                        continue
                    self._load(kind, p)
        for op in self.ops.values():
            if op.kind == "card":
                self.cards_by_mid[op.mid].append(op)
        for v in self.cards_by_mid.values():
            v.sort(key=lambda o: (o.day, o.oid))

    # ------------------------------------------------------------------ loading
    def _add_key(self, key: str, op: Op, grp: str | None = None) -> None:
        if key and len(key) >= 6:
            self.by_key[key].append((op, grp))

    def _load(self, kind: str, path: Path) -> None:
        df = pl.read_csv(path, infer_schema_length=0)
        if df.is_empty():
            return
        idcol = {"dd": "batch_id", "card": "settlement_id", "cheque": "deposit_id", "run": "run_id"}[kind]
        for (oid,), g in df.group_by(idcol, maintain_order=True):
            oid = str(oid)
            if (kind, oid) in self.ops:   # phase root first, first writer wins
                continue
            recs = g.to_dicts()
            r0 = recs[0]
            if kind == "dd":
                acct, d = r0["account_id"], day(r0["collection_date"])
                rows = [Row(r["remittance_info"] or "", cents(r["amount"]),
                            {"mandate": r["mandate_id"] or "", "iban": r["debtor_iban"] or "",
                             "name": r["debtor_name"] or ""}, r["end_to_end_id"] or str(i))
                        for i, r in enumerate(recs)]
                total = cents(r0["batch_total"])
            elif kind == "card":
                acct, d = r0["account_id"], day(r0["settlement_date"])
                rows = [Row(r["order_number"] or "", cents(r["gross"]), {"card": (r["card"] or "")[-4:]},
                            str(i), cents(r["gross"]), cents(r["net"])) for i, r in enumerate(recs)]
                total = sum(x.net for x in rows)
            elif kind == "cheque":
                acct, d = r0["account_id"], day(r0["deposit_date"])
                rows = [Row(r["stub_reference"] or "", cents(r["amount"]),
                            {"name": r["drawer"] or "", "cheque": r["cheque_number"] or ""}, str(i))
                        for i, r in enumerate(recs)]
                total = cents(r0["deposit_total"])
            else:
                acct, d = r0["debtor_account_id"], day(r0["execution_date"])
                rows = [Row(r["document_number"] or "", cents(r["document_amount"]),
                            {"name": r["beneficiary"] or "", "iban": r["beneficiary_iban"] or "",
                             "type": r["document_type"] or "", "ccy": r["currency"] or ""},
                            r["end_to_end_id"] or "")
                        for r in recs]
                total = 0
            op = Op(kind, oid, acct or "", (acct or "")[:4], d, abs(total), rows)
            op.ccy = str(r0.get("currency") or "")
            if kind == "run":
                for r, rec in zip(rows, recs):
                    op.groups.setdefault(r.group, abs(cents(rec["transfer_amount"])))
                op.total = sum(op.groups.values())
                for e2e in op.groups:
                    self._add_key(e2e, op, e2e)
            if kind == "card":
                op.mid = str(r0["merchant_id"] or "")
            self.ops[(kind, oid)] = op
            self._add_key(oid, op)
            for i, r in enumerate(rows):
                if kind == "dd" and r.party.get("mandate"):
                    self.dd_by_mandate[r.party["mandate"]].append((op, i))
                if kind == "cheque" and r.party.get("cheque"):
                    self.chq_by_number[r.party["cheque"].lstrip("0")].append((op, i))

    # ------------------------------------------------------------------ lookup
    def find(self, line: Line, text: str | None = None) -> list[tuple[Op, str | None]]:
        """Operations (and e2e group) a line names, most specific key first."""
        text = text if text is not None else line_text(line)
        hits: list[tuple[int, Op, str | None]] = []
        for tok in tokens_of(text):
            for op, grp in self.by_key.get(tok, ()):
                hits.append((len(tok), op, grp))
        # Spanish N43 run debits: ref2 holds the truncated run id ("PR..-0"), the trailing
        # 6-digit token of the text is the sequence -> group key "<run>-<seq:04d>".
        m = _RUN_REF.search(f"{line.ref2} {line.e2e or ''}")
        sq = _SEQ6.search((line.text or "").strip())
        if m and sq:
            for op, grp in self.by_key.get(f"{m[1]}-{int(sq[1]):04d}", ()):
                hits.append((99, op, grp))
        hits.sort(key=lambda h: (-h[0], h[1].kind, h[1].oid, h[2] or ""))
        seen, out = set(), []
        for _, op, grp in hits:
            k = (op.kind, op.oid, grp)
            if k not in seen:
                seen.add(k)
                out.append((op, grp))
        return out

    def card_ops(self, text: str, cash: int, d: int) -> list[Op]:
        """Card settlements for a MID printed in the text whose net total == cash."""
        out = []
        for mid in sorted(set(_MID.findall(text))):
            for op in self.cards_by_mid.get(mid, ()):
                if op.total == cash and abs(op.day - d) <= 10:
                    out.append(op)
        out.sort(key=lambda o: (abs(o.day - d), o.oid))
        return out

    # ------------------------------------------------------------------ resolve
    def resolve(self, line: Line, book) -> Hyp | None:
        text = line_text(line)
        cash = abs(line.cash)
        if cash == 0:
            return None
        sign = 1 if line.cash > 0 else -1
        m = _COUNT.search(text)
        want = int(m.group(1)) if m else 0
        named_e2e: set[str] = set()
        for op, grp in self.find(line, text):
            if (op.kind == "run") != (sign < 0):
                continue          # runs debit; dd/card/cheque credit
            if grp is None and op.oid in named_e2e:
                continue          # the line named one transfer: never subset the whole run
            if grp is not None:
                named_e2e.add(op.oid)
                rows = [r for r in op.rows if r.group == grp]
                gccy = rows[0].party.get("ccy", "") if rows else ""
                if gccy and line.ccy and gccy != line.ccy:
                    # cross-currency transfer: e2e is the identity, cash is in another ccy
                    hyp = self._hyp(op, rows, line, book)
                    if hyp is not None:
                        hyp.note += ";fx"
                        return hyp
                    continue
            rows = self._select(op, grp, cash, want, line, book)
            if rows is None:
                continue
            hyp = self._hyp(op, rows, line, book)
            if hyp is not None:
                return hyp
        if sign > 0:
            for op in self.card_ops(text, cash, line.day)[:1]:
                hyp = self._hyp(op, op.rows, line, book)
                if hyp is not None:
                    return hyp
        return None

    def _select(self, op: Op, grp: str | None, cash: int, want: int,
                line: Line | None = None, book=None) -> list[Row] | None:
        if grp is not None:
            return [r for r in op.rows if r.group == grp] if op.groups.get(grp) == cash else None
        if op.total == cash:
            return op.rows
        if op.kind == "run":
            keys = sorted(op.groups)
            pick = _subset([op.groups[k] for k in keys], cash, want if 0 < want < len(keys) else 0, op.oid.startswith("CF"))
            if pick is None:
                return None
            chosen = {keys[i] for i in pick}
            return [r for r in op.rows if r.group in chosen]
        if want and want < len(op.rows):
            pick = _subset([r.amt for r in op.rows], cash, want)
            if pick is not None:
                return [op.rows[i] for i in pick]
        return None

    def _hyp(self, op: Op, rows: list[Row], line: Line, book) -> Hyp | None:
        items, cn, adv, used, missing = [], [], {}, set(), 0
        for r in rows:
            idx = self.row_item(op, r, line, book, used)
            if idx is None:
                missing += 1
                continue
            used.add(idx)
            (cn if op.kind == "run" and r.amt < 0 else items).append(idx)
            adv[idx] = {"paid": abs(r.amt), "ded": 0, "reason": "", "src": "ops"}
        if not items and not cn:
            return None
        note = "card" if op.kind == "card" else op.kind
        if missing:
            note += f";missing={missing}/{len(rows)}"
        return Hyp(resolver=f"ops.{op.kind}", tier=0, items=items, cn=cn, advice=adv,
                   note=note)

    # ------------------------------------------------------------------ row -> item
    def scope(self, op: Op, line: Line) -> tuple[str, ...]:
        ent = line.entity or op.entity
        if op.kind == "run" and (line.account or op.account).startswith("ES00"):
            return AP_POOL
        return (ent,)

    def row_item(self, op: Op, r: Row, line: Line, book, used: set[int], closed_ok: bool = False) -> int | None:
        scope = self.scope(op, line)
        if op.kind == "run":
            kinds = ("APC",) if r.amt < 0 else ("API",)
        else:
            kinds = ("ARI",)
        amt = abs(r.amt)
        cands: list[int] = []
        docs = [r.doc] + [t for t in tokens_of(fold(r.doc)) if t != r.doc]
        for d in docs:
            if not d:
                continue
            for i in book.by_doc(d):
                it = book.items[i]
                if i in used or it.kind not in kinds or it.entity not in scope:
                    continue
                if not closed_ok and it.open <= 0:
                    continue
                cands.append(i)
            if cands:
                break
        if not cands and r.doc and hasattr(book, "by_doc_suffix"):
            for d in docs:
                if len(re.sub(r"[^A-Z0-9]", "", d.upper())) < 6:
                    continue
                cands = [i for i in book.by_doc_suffix(d, set(scope))
                         if i not in used and book.items[i].kind in kinds
                         and (closed_ok or book.items[i].open > 0)]
                if op.kind != "card" and len(cands) > 1:   # suffix: amount must disambiguate
                    cands = [i for i in cands if amt in (book.items[i].total, book.items[i].open)]
                if cands:
                    break
        if cands:
            return self._best(cands, amt, r, book)
        if op.kind in ("dd", "cheque") and not closed_ok:
            for party in self.parties(r, scope):
                pool = [i for i in book.open_of_party(party, kinds) if i not in used
                        and book.items[i].entity in scope]
                ex = [i for i in pool if book.items[i].open == amt]
                if ex:
                    return ex[0]
        return None

    def _best(self, cands: list[int], amt: int, r: Row, book) -> int | None:
        cands = sorted(set(cands))
        def key(i):
            it = book.items[i]
            amt_ok = 0 if it.total == amt else 1 if it.open == amt else 2
            nm = fold(r.party.get("name", ""))
            ben = 0 if nm and nm not in FACTORS and fold(it.party_name) == nm else 1
            return (amt_ok, ben, it.due, it.issue, it.item_id)
        ranked = sorted(cands, key=key)
        best = ranked[0]
        k0 = key(best)
        if k0[0] == 2 and len(cands) > 1:
            # partial payment (row amount != item amount): only a unique beneficiary match
            if k0[1] != 0 or key(ranked[1])[:2] == k0[:2]:
                return None
        return best

    def parties(self, r: Row, scope: tuple[str, ...]) -> list[str]:
        ms = getattr(self.phase, "masters", None)
        custs = getattr(ms, "customers", None) or {}
        idx = getattr(self, "_cidx", None)
        if idx is None:
            idx = {"mandate": {}, "iban": {}, "name": {}}
            for cid in sorted(custs):
                c = custs[cid]
                for k, v in (("mandate", c.get("mandate") or c.get("sepa_mandate_id")),
                             ("iban", c.get("iban")), ("name", fold(c.get("name")))):
                    if v:
                        idx[k].setdefault(v, []).append(cid)
            self._cidx = idx
        out: list[str] = []
        for k in ("mandate", "iban", "name"):
            v = r.party.get(k)
            v = fold(v) if k == "name" else v
            for cid in idx[k].get(v, ()) if v else ():
                if cid not in out and (custs[cid].get("entity") in scope or not custs[cid].get("entity")):
                    out.append(cid)
        return out


def _subset(amounts: list[int], target: int, want: int, big: bool = False) -> list[int] | None:
    """Unique exact subset (cardinality `want` if > 0, else ≤ 8) summing to target."""
    n = len(amounts)
    max_states = MAX_STATES * 7 if big else MAX_STATES
    if not n or n > (400 if big else MAX_GROUPS):
        return None
    limit = want or 8
    level: dict[int, tuple[int, ...] | None] = {0: ()}
    states = 0
    for depth in range(1, limit + 1):
        nxt: dict[int, tuple[int, ...] | None] = {}
        for tot, pick in level.items():
            if pick is None:
                continue
            start = pick[-1] + 1 if pick else 0
            for i in range(start, n):
                s = tot + amounts[i]
                if s > target:
                    continue
                if s in nxt:
                    nxt[s] = None   # ambiguous
                else:
                    nxt[s] = pick + (i,)
                states += 1
                if states > max_states:
                    return None
        if (not want or depth == want) and target in nxt:
            return list(nxt[target]) if nxt[target] is not None else None
        level = nxt
        if not level:
            break
    return None
