"""recon2 engine: one chronological pass composing every module (DESIGN §1).

Modules that are not importable yet are replaced by stubs (listed in `STATUS`), so the
pipeline always runs end to end; the stubs are deliberately conservative.
"""

from __future__ import annotations

import importlib
import os
import time
from pathlib import Path

from recon2 import resolve as R
from recon2.model import DATA, Alloc, Decision, Hyp, Line

STATUS: dict[str, str] = {}
ROOT = Path(__file__).resolve().parents[2]


def _try(mod: str):
    try:
        m = importlib.import_module(f"recon2.{mod}")
        STATUS[mod] = "integrated"
        return m
    except Exception as ex:  # missing or broken module -> stub
        STATUS[mod] = f"stub ({type(ex).__name__}: {str(ex)[:60]})"
        return None


# ---------------------------------------------------------------------- stubs
class StubBook:
    """Minimal Book (API.md) used only while recon2.book is missing."""

    def __init__(self, items, masters=None):
        import re
        self.items = items
        self.by_id = {it.item_id: it.idx for it in items}
        self.hist: dict[int, list] = {}
        self._doc: dict[str, list[int]] = {}
        self._party: dict[str, list[int]] = {}
        for it in items:
            for k in _doc_keys(it.doc) | _doc_keys(it.po):
                self._doc.setdefault(k, []).append(it.idx)
            self._party.setdefault(it.party_id, []).append(it.idx)
        self._keys = sorted(self._doc)
        self._rkeys = sorted((k[::-1], k) for k in self._doc)
        for v in self._party.values():
            v.sort(key=lambda i: (items[i].due, items[i].issue, items[i].doc, i))
        self._re = re

    def by_doc(self, token):
        out = []
        for k in sorted(_doc_keys(token)):
            out.extend(self._doc.get(k, []))
        return sorted(set(out))

    def _scope(self, idxs, scope):
        return sorted({i for i in idxs if scope is None or self.items[i].party_id in scope})

    def by_doc_suffix(self, token, scope=None):
        import bisect
        t = "".join(ch for ch in token.upper() if ch.isalnum())
        if len(t) < 6:
            return []
        r = t[::-1]
        out = []
        j = bisect.bisect_left(self._rkeys, (r, ""))
        while j < len(self._rkeys) and self._rkeys[j][0].startswith(r) and len(out) < 300:
            out.extend(self._doc[self._rkeys[j][1]])
            j += 1
        return self._scope(out, scope)

    def by_doc_prefix(self, token, scope=None):
        import bisect
        t = "".join(ch for ch in token.upper() if ch.isalnum())
        if len(t) < 7:
            return []
        out = []
        j = bisect.bisect_left(self._keys, t)
        while j < len(self._keys) and self._keys[j].startswith(t) and len(out) < 300:
            out.extend(self._doc[self._keys[j]])
            j += 1
        return self._scope(out, scope)

    def open_of_party(self, party, kinds):
        return [i for i in self._party.get(party, []) if self.items[i].open > 0 and self.items[i].kind in kinds]

    def open_by_amount(self, entity, ccy, cents):
        return []

    def settled(self, idx):
        return self.items[idx].open == 0 and bool(self.hist.get(idx))

    def apply(self, line, allocs):
        for a in allocs:
            it = self.items[a.item]
            if a.exc == "RETURNED":
                delta = -a.amount
            elif a.exc == "DUPLICATE_PAYMENT":
                delta = 0
            elif a.exc in ("SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES", "FX_DIFFERENCE", "OVERPAYMENT"):
                delta = it.open
            else:
                delta = min(a.amount, it.open)
            it.open = max(0, min(it.total, it.open - delta))
            it.residue = 0 < it.open < it.total
            self.hist.setdefault(a.item, []).append((line.idx, delta, a.exc, line.day))


def _doc_keys(s: str) -> set[str]:
    if not s:
        return set()
    a = "".join(ch for ch in s.upper() if ch.isalnum())
    out = {a} if len(a) >= 4 else set()
    digits = "".join(ch for ch in a if ch.isdigit())
    core = digits.lstrip("0")
    if len(core) >= 5:
        out.add(core)
    return out


def stub_split(line: Line, hyp: Hyp, book, ctx=None) -> dict[int, int] | None:
    items = book.items
    if not hyp.items:
        return None
    ccy = items[hyp.items[0]].ccy
    a = abs(line.cash)
    if ccy != line.ccy:
        if line.orig and line.orig_ccy == ccy:
            a = line.orig
        else:
            fx = getattr(getattr(ctx, "masters", None), "fx", None)
            if fx is None:
                return None
            a = fx.convert(a, line.ccy, ccy, line.day)
    if hyp.is_return:
        return {i: items[i].total for i in hyp.items}
    out = {}
    rem = a + sum(items[i].open for i in hyp.cn)
    for i in hyp.cn:
        out[i] = items[i].open
    order = [i for i in hyp.items if i != hyp.partial_item] + ([hyp.partial_item] if hyp.partial_item is not None else [])
    for k, i in enumerate(order):
        it = items[i]
        last = k == len(order) - 1
        if hyp.note == "dup":
            out[i] = a
            continue
        take = rem if last else min(rem, it.open)
        if take <= 0:
            return None
        out[i] = take
        rem -= take
    return out


def stub_decide(line: Line, hyp: Hyp, book, ctx, applied: dict[int, int]) -> list[Alloc]:
    items = book.items
    out = []
    multi = len(hyp.items) > 1
    for i, amt in applied.items():
        it = items[i]
        if hyp.is_return:
            out.append(Alloc(i, it.total, "RETURNED", "E1"))
        elif hyp.note == "dup":
            out.append(Alloc(i, amt, "DUPLICATE_PAYMENT", "E2"))
        elif i in hyp.cn:
            out.append(Alloc(i, it.open, "", "E3"))
        elif hyp.resolver.startswith("ops.card"):
            out.append(Alloc(i, it.open, "", "E0"))
        elif amt >= it.open:
            cross = it.ccy != line.ccy
            exc = "FX_DIFFERENCE" if cross else ("OVERPAYMENT" if amt > it.open and not multi and it.kind == "ARI" else "")
            out.append(Alloc(i, it.open, exc, "E5"))
        else:
            if R.Resolver._short_shape(_SHAPE, line, it) if _SHAPE else False:
                out.append(Alloc(i, it.open, "SHORT_DISCOUNT", "E7s"))
            else:
                out.append(Alloc(i, amt, "PARTIAL", "E10"))
    return out


_SHAPE = None


# ---------------------------------------------------------------------- engine
class Engine:
    def __init__(self, phase_name: str, log=print):
        self.log = log
        t0 = time.perf_counter()
        self.timings: dict[str, float] = {}
        load = _try("load")
        if load is None:
            raise RuntimeError("recon2.load is required: " + STATUS["load"])
        self.phase = ph = load.load_phase(phase_name)
        self.timings["load"] = time.perf_counter() - t0
        t = time.perf_counter()
        tv = _try("textviews")
        if tv is not None and ph.lines and not ph.lines[0].t_join:
            for ln in ph.lines:
                tv.build(ln)
        carry_hist = _sample_carry(ph) if (phase_name == "sample" or phase_name.startswith("train:")) and not os.environ.get("RECON2_NO_CARRY") else {}
        bookm = _try("book")
        self.book = bookm.Book(ph.items, ph.masters) if bookm else StubBook(ph.items, ph.masters)
        for i, h in carry_hist.items():
            self.book.hist[i] = h
        self.timings["book"] = time.perf_counter() - t
        t = time.perf_counter()
        self.treasury = _try("treasury")
        cats = _try("cats")
        self.router = None
        if cats is not None:
            art = ROOT / "artifacts" / "router.json"
            try:
                self.router = cats.Router(art)
            except Exception as ex:
                STATUS["cats"] = f"stub (Router: {ex})"
        pdfc = _try("pdfclauses")
        facts, notices = {}, None
        if pdfc is not None:
            try:
                facts = pdfc.load_facts(ph.root)
                notices = pdfc.notices(facts) if hasattr(pdfc, "notices") else None
            except Exception as ex:
                STATUS["pdfclauses"] = f"stub (load_facts: {type(ex).__name__}: {ex})"
        party = _try("party")
        self.parties = None
        if party is not None:
            try:
                # RECON2_ALIASES lets the sample be scored with a holdout (<=2024-12) alias table.
                aliases = Path(os.environ.get("RECON2_ALIASES", ROOT / "artifacts" / "aliases.json"))
                self.parties = party.Parties(ph.masters, aliases if aliases.exists() else None, notices)
            except Exception as ex:
                STATUS["party"] = f"stub (Parties: {type(ex).__name__}: {ex})"
        self.facts = facts
        self.res = R.Resolver(self.book, ph.masters, self.parties, facts)
        global _SHAPE
        _SHAPE = self.res
        self.extra = [e for e in {"sample": ["train"], "live": ["train"], "final": ["live"]}.get(phase_name, [])
                      if (DATA / e).exists()]
        opsm = _try("ops")
        self.ops = None
        if opsm is not None:
            extra = self.extra
            try:
                self.ops = opsm.Ops(ph, [DATA / e for e in extra if (DATA / e).exists()])
            except Exception as ex:
                STATUS["ops"] = f"stub (Ops: {type(ex).__name__}: {ex})"
        retm = _try("returns")
        self.returns = None
        if retm is not None and self.ops is not None:
            try:
                self.returns = retm.Returns(ph, self.ops)
            except Exception as ex:
                STATUS["returns"] = f"stub (Returns: {type(ex).__name__}: {ex})"
        advm = _try("advices")
        self.advices = None
        if advm is not None and hasattr(advm, "AdviceIndex"):
            try:
                advs = list(advm.load_advices(ph.root))
                start = min((ln.day for ln in ph.lines), default=0)
                for e in self.extra:
                    # advices received before the phase for payments inside it (H7: up to -8d)
                    advs += [a for a in advm.load_advices(DATA / e) if a.payment_date >= start]
                self.advices = advm.AdviceIndex(advs)
            except Exception as ex:
                STATUS["advices"] = f"stub (AdviceIndex: {type(ex).__name__}: {ex})"
        elif advm is not None:
            STATUS["advices"] = "stub (no AdviceIndex)"
        dec = _try("decide")
        self.split, self.decide, self.ctx = stub_split, stub_decide, None
        if dec is not None:
            try:
                self.ctx = dec.Ctx(ph.masters, _wht_rates(), self._plans())
                self.split, self.decide = dec.split_cash, dec.decide
            except Exception as ex:
                STATUS["decide"] = f"stub (Ctx: {type(ex).__name__}: {ex})"
        self.timings["modules"] = time.perf_counter() - t
        self.item_ids = [it.item_id for it in self.book.items]
        self.errors: dict[str, int] = {}

    def _plans(self) -> dict[int, int]:
        """item idx -> instalment count from PDF facts (key (entity, doc) or (entity, supplier|doc))."""
        out = {}
        for (ent, key), f in sorted(self.facts.items()):
            ins = f.get("instalments") if isinstance(f, dict) else None
            if not ins or not ins.get("n"):
                continue
            sup, _, doc = key.rpartition("|")
            for i in self.book.by_doc(doc):
                it = self.book.items[i]
                if it.entity == ent and (not sup or it.party_id == sup):
                    out[i] = int(ins["n"])
        return out

    # ------------------------------------------------------------------
    def _call(self, name, fn, *a):
        try:
            return fn(*a)
        except Exception as ex:
            k = f"{name}:{type(ex).__name__}:{str(ex)[:50]}"
            self.errors[k] = self.errors.get(k, 0) + 1
            return None

    def advice_hyp(self, line: Line, pset: set[str], trace: dict) -> Hyp | None:
        if self.advices is None:
            return None
        advs = self._call("advices.match", self.advices.match, line) or []
        if not advs:
            return None
        items, cns, facts = [], [], {}
        for adv in advs[:2]:
            unresolved = 0
            for row in adv.rows:
                hits = [i for i in self.book.by_doc(row.doc) if self.res.eligible(line, i, allow_cross_side=True)]
                if not hits and len(row.doc) >= 6:
                    hits = [i for i in self.book.by_doc_suffix(row.doc, pset or None) if self.res.eligible(line, i, True)]
                if len(hits) > 1 and pset:
                    hits = [i for i in hits if self.book.items[i].party_id in pset] or hits
                if len(hits) > 1:
                    g = [i for i in hits if self.book.items[i].total == abs(row.gross)]
                    hits = g or hits
                if len(hits) != 1:
                    unresolved += 1
                    continue
                i = hits[0]
                if i in facts:
                    continue
                facts[i] = {"paid": row.paid, "ded": row.ded, "reason": row.reason, "gross": row.gross}
                (cns if self.book.items[i].kind in ("ARC", "APC") or row.paid < 0 else items).append(i)
            if items and not unresolved:
                return Hyp("advice", 0, items, cns, advice=facts)
            if unresolved:
                trace.setdefault("caps", []).append("ADVICE_ROW_UNRESOLVED")
            items, cns, facts = [], [], {}
        return None

    def line(self, line: Line) -> Decision:
        d = Decision(line.txn)
        tr = d.trace
        # 1. treasury (final)
        if self.treasury is not None:
            cat = self._call("treasury", self.treasury.classify, line)
            if cat:
                d.category = cat
                tr.update(route="treasury", reason="TREASURY")
                return d
        hyps: list[Hyp] = []
        pairs = self.res.party_set(line)
        pset = {p for p, _ in pairs}
        tr["parties"] = [list(p) for p in pairs[:10]]
        # 2. T0/T1
        if self.returns is not None:
            h = self._call("returns.resolve", self.returns.resolve, line, self.book)
            if h:
                hyps.append(h)
        if self.ops is not None and not hyps:
            h = self._call("ops.resolve", self.ops.resolve, line, self.book)
            if h:
                hyps.append(h)
        if not hyps:
            h = self.advice_hyp(line, pset, tr)
            if h:
                hyps.append(h)
        if self.returns is not None and not hyps and hasattr(self.returns, "represent"):
            h = self._call("returns.represent", self.returns.represent, line, self.book, sorted(pset))
            if h:
                hyps.append(h)
        # refund of an earlier duplicate -> category
        if not hyps:
            cat = self.res.refund_category(line)
            if cat:
                d.category = cat
                tr.update(route="refund", reason="ROUTER")
                return d
        # 3. refs and party+amount
        if not hyps:
            hyps += self._call("refs", self.res.by_ref, line, pset, tr) or []
        if not hyps or all(R.fit_level(self.res, line, h) < 2 for h in hyps):
            hyps += self._call("party", self.res.by_party_amount, line, [p for p, _ in pairs], tr, frozenset(), frozenset(p for p, s in pairs if s.startswith("child:"))) or []
        if not hyps and pset:
            h = self._call("refund_cn", self.res.refund_cn, line, pset)
            if h:
                hyps.append(h)
        # 4. arbitration + decide
        ranked = sorted([h for h in hyps if h and h.items], key=lambda h: (
            -R.fit_level(self.res, line, h), h.tier, len(h.items),
            min(self.book.items[i].due for i in h.items), tuple(self.item_ids[i] for i in h.items)))
        for h in ranked[:4]:
            applied = self._call("split_cash", self.split, line, h, self.book, self.ctx)
            if not applied:
                tr.setdefault("caps", []).append(f"AMOUNT_NO_FIT:{h.resolver}")
                continue
            allocs = self._call("decide", self.decide, line, h, self.book, self.ctx, applied)
            allocs = [a for a in (allocs or []) if a.amount != 0]   # a 0-amount row cannot exist in golden
            if not allocs:
                continue
            why = __import__("recon2.writer", fromlist=["guard"]).guard(allocs, self.item_ids)
            if why:
                tr.setdefault("caps", []).append("GUARD_DEMOTED:" + why)
                continue
            self.book.apply(line, allocs)
            if any(a.exc == "DUPLICATE_PAYMENT" for a in allocs):
                self.res.note_dup(line)
            d.allocs = allocs
            tr.update(route="item", resolver=h.resolver, tier=h.tier)
            return d
        # 5. fallback category
        reason = tr.get("ref") or tr.get("party") or ("NO_PARTY" if not pset else "AMOUNT_NO_FIT")
        cat = None
        if self.router is not None:
            cat = self._call("router", self.router.category, line)
        if not cat:
            cat = "UNIDENTIFIED" if line.cash > 0 else "BANK_FEES"
        d.category = cat
        strong = any(src in ("iban", "id8", "alias") for _, src in pairs)
        d.exc = "UNIDENTIFIED" if cat == "UNIDENTIFIED" and not (strong and tr.get("cands")) else ""
        tr.update(route="category", reason=reason if line.cash > 0 else "ROUTER")
        return d

    def run(self) -> list[Decision]:
        import sys
        t = time.perf_counter()
        lines = self.phase.lines
        n = len(lines)
        every = int(os.environ.get("RECON2_PROGRESS", "5000"))
        out: list[Decision] = []
        last, slow = t, []
        for i, ln in enumerate(lines):
            t1 = time.perf_counter()
            out.append(self.line(ln))
            dt = time.perf_counter() - t1
            if dt > 1.0:  # progress/diagnostics only; never affects output
                slow.append((dt, ln.txn))
                print(f"[recon2] SLOW {dt:.1f}s {ln.txn} cash={ln.cash} text={ln.text[:60]!r}", file=sys.stderr, flush=True)
            if every and (i + 1) % every == 0:
                now = time.perf_counter()
                items = sum(1 for d in out[-every:] if d.allocs)
                print(f"[recon2] progress {i+1}/{n} ({100*(i+1)/n:.0f}%) elapsed={now-t:.0f}s "
                      f"rate={every/max(now-last,1e-9):.0f} l/s items={items}/{every} states={self.res.states}",
                      file=sys.stderr, flush=True)
                last = now
        self.timings["match"] = time.perf_counter() - t
        if slow:
            print(f"[recon2] {len(slow)} slow lines, worst {max(slow)[0]:.1f}s", file=sys.stderr, flush=True)
        return out


def _sample_carry(ph) -> dict[int, list]:
    """The sample is June 2025 cut out of TRAIN: its item tables carry full totals although
    many items were part-paid in May.  Emulate LIVE's open_items_at_start by replaying the
    TRAIN golden of lines booked before the sample (labelled training data, sample only)."""
    import polars as pl
    if not ph.lines:
        return {}
    first = min(ln.txn for ln in ph.lines)
    start = min(ln.day for ln in ph.lines)
    by_id = {it.item_id: it for it in ph.items}
    g = pl.read_csv(DATA / "train" / "golden_allocations.csv.gz", columns=["bank_txn_id", "item_id", "amount", "exception"],
                    schema_overrides={"amount": pl.Utf8})
    g = g.filter(pl.col("item_id").is_in(list(by_id)) & (pl.col("bank_txn_id") < first))
    if g.is_empty():
        return {}
    bt = pl.read_csv(DATA / "train" / "bank_transactions.csv.gz", columns=["bank_txn_id", "booking_date"])
    # Strictly BEFORE the window by date (ids are not date-ordered across accounts: review HIGH 3).
    from recon2.model import iso as _iso
    g = (g.join(bt, on="bank_txn_id", how="left").filter(pl.col("booking_date") < _iso(start))
         .sort(["booking_date", "bank_txn_id", "item_id"]))
    from recon2.model import day as _day
    hist: dict[int, list] = {}
    for txn, iid, amt, exc, bd in g.iter_rows():
        it = by_id[iid]
        a = _cents(amt)
        d = _day(bd) if bd else start - 1
        if exc == "DUPLICATE_PAYMENT":
            delta = 0
        elif exc == "RETURNED":
            delta = -a
        elif exc in ("PARTIAL", "SHORT_DISPUTE", ""):
            delta = min(a, it.open)
        else:
            delta = it.open
        it.open = max(0, min(it.total, it.open - delta))
        it.residue = 0 < it.open < it.total
        hist.setdefault(it.idx, []).append((-1, delta, exc or "", d))
    return hist


def _cents(s: str) -> int:
    s = s.strip()
    neg = s.startswith("-")
    s = s.lstrip("-")
    w, _, f = s.partition(".")
    v = int(w or 0) * 100 + int((f + "00")[:2] or 0)
    return -v if neg else v


def _wht_rates() -> dict[str, int]:
    p = ROOT / "artifacts" / "wht.json"
    if p.exists():
        import json
        return {k: int(v) for k, v in json.loads(p.read_text()).items()}
    return {}


def run(phase_name: str) -> list[Decision]:
    return Engine(phase_name).run()
