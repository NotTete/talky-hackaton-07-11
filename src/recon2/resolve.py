"""Reference (T2/T3) and party+amount (T4/T5) resolvers, duplicates, refund pairing,
fit test and arbitration (DESIGN §1, §3).

Everything here proposes `Hyp`s; `decide()` writes the amounts.  A Hyp only leaves this
module when its arithmetic fits the cash (`fit() > 0`).
"""

from __future__ import annotations

import re
from datetime import date

from recon2.model import Hyp, Item, Line

TOL_PCT = 0.005
AP_HOLDING_SCOPE = {"ES00", "ES01", "ES02", "PT01"}
WHT_BP = (125, 150, 1000)
WHT_RATES = {"BR01": (150, 1000), "MA01": (150, 1000), "MX01": (125, 1000)}
FANOUT = 250
SUB_CANDS, SUB_PICKS, SUB_STATES = 40, 12, 200_000
BITSET_MAX = 20_000_000  # cents; above this the bounded dict DP is used
LINE_SUBSET_CALLS = 24  # deterministic per-line work budget (LIVE key accounts blew up to 9 s/line)

MONTHS = {}
for i, names in enumerate([
    ("ENERO", "JANUARY", "JANVIER", "JANUAR", "GENNAIO", "JANEIRO"),
    ("FEBRERO", "FEBRUARY", "FEVRIER", "FEBRUAR", "FEBBRAIO", "FEVEREIRO"),
    ("MARZO", "MARCH", "MARS", "MARZ", "MARCO"),
    ("ABRIL", "APRIL", "AVRIL", "APRILE"),
    ("MAYO", "MAY", "MAI", "MAGGIO", "MAIO"),
    ("JUNIO", "JUNE", "JUIN", "JUNI", "GIUGNO", "JUNHO"),
    ("JULIO", "JULY", "JUILLET", "JULI", "LUGLIO", "JULHO"),
    ("AGOSTO", "AUGUST", "AOUT"),
    ("SEPTIEMBRE", "SEPTEMBER", "SEPTEMBRE", "SETTEMBRE", "SETEMBRO"),
    ("OCTUBRE", "OCTOBER", "OCTOBRE", "OKTOBER", "OTTOBRE", "OUTUBRO"),
    ("NOVIEMBRE", "NOVEMBER", "NOVEMBRE", "NOVEMBRO"),
    ("DICIEMBRE", "DECEMBER", "DECEMBRE", "DEZEMBER", "DICEMBRE", "DEZEMBRO"),
], start=1):
    for n in names:
        MONTHS[n] = i
MONTH_RE = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b[ /.-]*(?:DE |OF )?(20\d\d|\d\d)?")


def tol(c: int) -> int:
    return max(1, int(abs(c) * TOL_PCT))


def side_kinds(line: Line) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(invoice kinds, credit-note kinds) settled by a line of this sign."""
    return (("ARI",), ("ARC",)) if line.cash > 0 else (("API",), ("APC",))


def entity_scope(line: Line, kind: str) -> set[str]:
    if kind in ("API", "APC") and line.entity == "ES00":
        return AP_HOLDING_SCOPE
    return {line.entity}


def _load_wht() -> dict[str, int]:
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "artifacts" / "wht.json"
    return {k: int(v) for k, v in json.loads(p.read_text()).items()} if p.exists() else {}


class Resolver:
    def __init__(self, book, masters=None, parties=None, pdf_facts: dict | None = None, fx=None):
        self.book = book
        self.items: list[Item] = book.items
        self.masters = masters
        self.parties = parties
        self.fx = fx if fx is not None else getattr(masters, "fx", None)
        self.terms = getattr(masters, "terms", {}) or {}
        self.customers = getattr(masters, "customers", {}) or {}
        self.entity_country = getattr(masters, "entity_country", {}) or {}
        self.children: dict[str, list[str]] = {}
        for cid, c in sorted(self.customers.items()):
            p = c.get("parent") or ""
            if p:
                self.children.setdefault(p, []).append(cid)
        # PDF "anula y sustituye": replaced doc -> replacement item idxs
        self.replaced: dict[str, list[int]] = {}
        for (ent, doc), f in sorted((pdf_facts or {}).items()):
            rep = f.get("replaces") if isinstance(f, dict) else None
            if not rep:
                continue
            rep = rep.get("doc_no") if isinstance(rep, dict) else rep
            if not rep:
                continue
            for i in book.by_doc(doc.split("|")[-1]):
                if self.items[i].entity == ent:
                    self.replaced.setdefault(_alnum(rep), []).append(i)
        # invoice PDF "less advance ... net amount due": the net is the payable cash (PARTIAL)
        self.adv_net: dict[int, int] = {}
        for (ent, doc), f in sorted((pdf_facts or {}).items()):
            adv = f.get("advance") if isinstance(f, dict) else None
            if not adv or not adv.get("net"):
                continue
            sup, _, d = doc.rpartition("|")
            for i in book.by_doc(d):
                if self.items[i].entity == ent and (not sup or self.items[i].party_id == sup):
                    self.adv_net[i] = int(adv["net"])
        self.wht_learned = _load_wht()
        self.dups: list[tuple[str, int, int, str]] = []   # (account, |cash|, day, side) of DUP lines
        self.states = 0

    # ------------------------------------------------------------------ money
    def cash_in(self, line: Line, ccy: str) -> tuple[int, int, int] | None:
        """|cash| expressed in item ccy: (point, lo, hi).  None if not convertible."""
        a = abs(line.cash)
        if ccy == line.ccy:
            return a, a, a
        if line.orig and line.orig_ccy == ccy:
            return line.orig, line.orig, line.orig
        if self.fx is None:
            return None
        try:
            p = self.fx.convert(a, line.ccy, ccy, line.day)
        except Exception:
            return None
        return p, int(p * 0.97), int(p * 1.03) + 1

    def fits_sum(self, line: Line, ccy: str, target: int, exact_only: bool = False) -> int:
        """3 exact, 2 within scorer tolerance / FX band, 0 no."""
        c = self.cash_in(line, ccy)
        if c is None:
            return 0
        p, lo, hi = c
        if lo == hi:
            if p == target:
                return 3
            if not exact_only and abs(p - target) <= tol(target):
                return 2
            return 0
        return 2 if lo <= target <= hi else 0

    # ------------------------------------------------------------------ eligibility
    def eligible(self, line: Line, i: int, allow_cross_side: bool = False) -> bool:
        it = self.items[i]
        inv, cn = side_kinds(line)
        if it.kind not in inv + cn:
            if not allow_cross_side or it.kind not in ("ARC", "APC"):
                return False
        return it.entity in entity_scope(line, it.kind)

    def party_set(self, line: Line, side: str | None = None) -> list[tuple[str, str]]:
        if self.parties is None:
            return []
        side = side or ("AR" if line.cash > 0 else "AP")
        try:
            ps = self.parties.resolve(line, side)
        except Exception:
            return []
        out, seen = [], set()
        for pid, src in ps:
            for q in [pid] + (self.children.get(pid, []) if side == "AR" else []):
                if q not in seen:
                    seen.add(q)
                    out.append((q, src if q == pid else "child:" + src))
        return out

    # ------------------------------------------------------------------ refs (T2/T3)
    def named(self, line: Line, pset: set[str], trace: dict) -> tuple[list[int], int]:
        """Items named by reference tokens, in text order; tier 2 exact, 3 noisy."""
        found: list[tuple[int, int, int]] = []  # (pos, tier, idx)
        tj = line.t_join or ""
        toks = list(line.tokens)
        try:
            for h in self.parties.third_party_doc_hint(line) if self.parties else []:
                if h not in toks:
                    toks.append(h)
        except Exception:
            pass
        tier = 9
        per_tok = []
        for tok in toks:
            pos = tj.find(tok)
            pos = pos if pos >= 0 else 10_000
            hits = [i for i in self.book.by_doc(tok) if self.eligible(line, i, True)]
            t = 2
            rep = [i for i in self.replaced.get(_alnum(tok), []) if self.eligible(line, i)]
            if rep:
                hits, t = rep, 2
            if not hits and len(tok) >= 6:
                hits = [i for i in self.book.by_doc_suffix(tok, pset or None) if self.eligible(line, i, True)]
                t = 3
            if not hits and len(tok) >= 7:
                hits = [i for i in self.book.by_doc_prefix(tok, pset or None) if self.eligible(line, i)]
                t = 3
            if not hits:
                continue
            if len(hits) > 1 and pset:
                hp = [i for i in hits if self.items[i].party_id in pset]
                hits = hp or hits
            if len(hits) > 1:
                ho = [i for i in hits if self.items[i].open > 0]
                hits = ho or hits
            per_tok.append((pos, t, tok, hits))
        # dominant party = party of the unambiguous hits; it disambiguates the rest
        cnt: dict[str, int] = {}
        for pos, t, tok, hits in per_tok:
            if len(hits) == 1:
                cnt[self.items[hits[0]].party_id] = cnt.get(self.items[hits[0]].party_id, 0) + 1
        dom = max(sorted(cnt), key=lambda p: cnt[p]) if cnt else None
        self.last_dom = dom
        for pos, t, tok, hits in per_tok:
            if len(hits) > 1 and dom:
                hits = [i for i in hits if self.items[i].party_id == dom] or hits
            if len(hits) > 1 and pset:   # truncated ref, several of the payer's invoices: the amount decides
                own = [i for i in hits if self.items[i].open > 0 and self.items[i].party_id in pset]
                for ok in (lambda x: self.fits_sum(line, x.ccy, x.open) == 3, lambda x: self._short_shape(line, x),
                           lambda x: self.fits_sum(line, x.ccy, x.open)):   # exact, then explained short, then tolerance
                    am = [i for i in own if ok(self.items[i])]
                    if am:
                        if len(am) == 1:
                            hits = am
                        break
            if len(hits) > 1 and dom and len(per_tok) > 1:
                trace.setdefault("caps", []).append("REF_AMBIGUOUS:" + tok)
                continue
            if len(hits) > 3:
                trace.setdefault("caps", []).append("REF_AMBIGUOUS:" + tok)
                continue
            for i in hits:
                found.append((pos, t, i))
                tier = min(tier, t)
        found.sort()
        out, seen = [], set()
        for _, _, i in found:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out, tier

    def by_ref(self, line: Line, pset: set[str], trace: dict) -> list[Hyp]:
        named, tier = self.named(line, pset, trace)
        if not named:
            trace["ref"] = "NO_REF" if not line.tokens else "REF_NOT_FOUND"
            return []
        for i in named[:10]:
            trace.setdefault("cands", []).append([self.items[i].item_id, "ref", "named"])
        inv_k, cn_k = side_kinds(line)
        open_inv = [i for i in named if self.items[i].kind in inv_k and self.items[i].open > 0]
        closed = [i for i in named if self.items[i].kind in inv_k and self.items[i].open == 0]
        cns = [i for i in named if self.items[i].kind in cn_k and self.items[i].open > 0]
        hyps = []
        # duplicate: a single named item already settled, same amount
        if not open_inv and len(closed) == 1:
            h = self.dup_hyp(line, closed[0])
            if h:
                return [h]
            trace["ref"] = "ITEM_CLOSED"
            return []
        if not open_inv and not cns:
            # cross-side credit note alone: refunded APC (credit) / paid-out ARC (debit)
            xcn = [i for i in named if self.items[i].kind not in inv_k + cn_k and self.items[i].open > 0
                   and self.fits_sum(line, self.items[i].ccy, self.items[i].open)]
            if len(xcn) == 1:
                return [Hyp("ref.xcn", tier, xcn, note="xcn")]
            trace["ref"] = "ITEM_CLOSED"
            return []
        if not open_inv and cns:
            return []
        ccys = {self.items[i].ccy for i in open_inv + cns}
        if len(ccys) != 1:
            trace["ref"] = "AMOUNT_NO_FIT"
            return []
        h = self.fit_named(line, open_inv, cns, pset, tier)
        if h:
            hyps.append(h)
        else:
            trace["ref"] = "AMOUNT_NO_FIT"
        return hyps

    def fit_named(self, line: Line, inv: list[int], cns: list[int], pset: set[str], tier: int) -> Hyp | None:
        it = self.items
        ccy = it[inv[0]].ccy
        c = self.cash_in(line, ccy)
        if c is None:
            return None
        cash, lo, hi = c
        s_open = sum(it[i].open for i in inv) - sum(it[i].open for i in cns)
        s_tot = sum(it[i].total for i in inv) - sum(it[i].open for i in cns)
        # 1) the whole named set fits (open or total within tolerance / band)
        if self.fits_sum(line, ccy, s_open) or (s_tot != s_open and self.fits_sum(line, ccy, s_tot)):
            return Hyp("ref", tier, list(inv), list(cns), note="all")
        # 2) party credit notes (<=3) explain the gap
        if pset and cash < s_open:
            dom = {it[i].party_id for i in inv}
            extra = self._party_cns(line, dom if len(dom) == 1 else pset, ccy, exclude=set(cns))
            pick = _subset_exact([it[i].open for i in extra], s_open - cash, 3, self)
            if pick is not None:
                return Hyp("ref.cn", tier, list(inv), list(cns) + [extra[k] for k in pick], note="cn")
        # 3) discount / withholding / short shapes -> let decide() judge (named items)
        if cash < s_open:
            if len(inv) == 1:
                return Hyp("ref", tier, list(inv), list(cns), partial_item=None, note="short1")
            # n_to_m: full rows first, single partial on the last named item
            if lo == hi:
                # exact subset of the named items (some named docs not paid)
                pick = _subset_exact([it[i].open for i in inv], cash + sum(it[i].open for i in cns), SUB_PICKS, self)
                if pick is not None and len(pick) < len(inv):
                    return Hyp("ref.subset", tier, [inv[k] for k in pick], list(cns), note="subset")
            rest = sum(it[i].open for i in inv[:-1]) - sum(it[i].open for i in cns)
            if rest < cash:
                return Hyp("ref.n_to_m", tier, list(inv), list(cns), partial_item=inv[-1], note="n_to_m")
            return Hyp("ref.short", tier, list(inv), list(cns), note="short")
        # cash > named open
        if len(inv) == 1 and not cns and line.cash > 0 and tier == 2 and (cash - s_open) * 10 <= s_open:
            return Hyp("ref", tier, list(inv), [], note="over")
        return None

    def _party_cns(self, line: Line, pset: set[str], ccy: str, exclude: set[int]) -> list[int]:
        _, cn_k = side_kinds(line)
        out = []
        for p in sorted(pset):
            for i in self.book.open_of_party(p, cn_k):
                if i not in exclude and self.items[i].ccy == ccy and self.eligible(line, i):
                    out.append(i)
        return out[:12]

    # ------------------------------------------------------------------ duplicates / refunds
    def dup_hyp(self, line: Line, i: int) -> Hyp | None:
        it = self.items[i]
        hist = self.book.hist.get(i) or []
        if not hist or it.open != 0:
            return None
        last_day = max(h[3] for h in hist)
        win = 15 if it.kind.startswith("AR") else 50
        if line.day - last_day > win:
            return None
        if not self.fits_sum(line, it.ccy, it.total) and not any(
                self.fits_sum(line, it.ccy, abs(h[1])) for h in hist):
            return None
        return Hyp("dup", 1, [i], note="dup")

    def note_dup(self, line: Line) -> None:
        self.dups.append((line.account, abs(line.cash), line.day, "AR" if line.cash > 0 else "AP"))

    def refund_category(self, line: Line) -> str | None:
        a = abs(line.cash)
        if line.cash < 0:
            want, win = "AR", 47
            cat = "CUSTOMER_REFUND"
        else:
            want, win = "AP", 50
            cat = "SUPPLIER_REFUND"
        for acc, amt, d, side in reversed(self.dups):
            if line.day - d > win:
                break
            if side == want and amt == a and (acc == line.account or want == "AP"):
                self.dups.remove((acc, amt, d, side))
                return cat
        return None

    def refund_cn(self, line: Line, pset: set[str]) -> Hyp | None:
        """Credit equal to one open APC of the payer (supplier refund of a credit note), or
        debit equal to one open ARC (credit note paid out)."""
        kinds = ("APC",) if line.cash > 0 else ("ARC",)
        hits = []
        for p in sorted(pset):
            for i in self.book.open_of_party(p, kinds):
                it = self.items[i]
                if it.entity in entity_scope(line, it.kind) and self.fits_sum(line, it.ccy, it.open, exact_only=True):
                    hits.append(i)
        if len(hits) >= 1:
            hits.sort(key=lambda i: (self.items[i].issue, self.items[i].item_id))
            return Hyp("refund.cn", 4, [hits[0]], note="cn_refund")
        return None

    # ------------------------------------------------------------------ party + amount (T4/T5)
    def by_party_amount(self, line: Line, pset, trace: dict, reopened: set[int] = frozenset(), child: frozenset = frozenset()) -> list[Hyp]:
        """pset: party ids in precedence order (list) or a set."""
        plist = list(dict.fromkeys(pset if isinstance(pset, list) else sorted(pset)))
        if not plist:
            trace["party"] = "NO_PARTY"
            return []
        inv_k, cn_k = side_kinds(line)
        it = self.items
        per: dict[str, list[int]] = {}
        for p in plist:
            per[p] = [i for i in self.book.open_of_party(p, inv_k) if self.eligible(line, i)
                      and not it[i].residue and 0 <= line.day - it[i].issue <= 365]
        cands = list(dict.fromkeys(i for p in plist for i in per[p]))
        if not cands:
            trace["party"] = "NO_OPEN_ITEM"
            return []
        for i in cands[:10]:
            trace.setdefault("cands", []).append([it[i].item_id, "party", "open"])
        dcands = [i for i in cands if it[i].party_id not in child]   # children: subsets only, never amount/fifo/short
        # exact / tolerant single item (cheap over the whole set: no fanout cap needed)
        exact = [i for i in dcands if self.fits_sum(line, it[i].ccy, it[i].open)]
        if exact:
            best = max(self.fits_sum(line, it[i].ccy, it[i].open) for i in exact)
            exact = [i for i in exact if self.fits_sum(line, it[i].ccy, it[i].open) == best]
            if best < 3 and len(plist) > 1 and not any(self.fits_sum(line, it[i].ccy, it[i].open) == 3 for i in cands):
                exact = []   # tolerance-only coincidence across an ambiguous party set is not evidence
            if len(exact) == 1:
                return [Hyp("party.amount", 4, exact, note="unique")]
            # several equal: keep the highest-precedence party that has one
            for p in plist:
                ex = [i for i in exact if it[i].party_id == p]
                if ex:
                    exact = ex
                    break
            if len(exact) == 1:
                return [Hyp("party.amount", 4, exact, note="unique_p")]
            if exact:
                pick = self._month_pick(line, exact) or self._oldest(exact)
                return [Hyp("party.fifo", 5, [pick], note="fifo")]
        # single-item short shapes (discount / withholding / bank charges)
        shp = [i for i in dcands if self._short_shape(line, it[i])]
        if not shp:
            # first payment of a retention (0.95/0.90) or an instalment plan piece (total/n)
            shp = [i for i in dcands if self._piece_shape(line, it[i])]
        if len(shp) == 1:
            return [Hyp("party.short", 4, shp, note="short")]
        if len(shp) > 1:
            for p in plist:
                sp = [i for i in shp if it[i].party_id == p]
                if sp:
                    return [Hyp("party.short", 5, [self._oldest(sp)], note="short_fifo")]
        # subsets: one party at a time (names are a SET, a sum across parties is coincidence)
        for p in plist:
            cs_all = per[p]
            if len(cs_all) < 2:
                continue
            if len(cs_all) > FANOUT:
                trace.setdefault("caps", []).append("FANOUT_CAP")
                continue
            by_ccy: dict[str, list[int]] = {}
            for i in cs_all:
                by_ccy.setdefault(it[i].ccy, []).append(i)
            for ccy, cs in sorted(by_ccy.items()):
                c = self.cash_in(line, ccy)
                if c is None or c[0] != c[1]:
                    continue
                cash = c[0]
                cs = sorted(cs, key=lambda i: (it[i].due, it[i].issue, it[i].item_id))
                # whole open book of the party / oldest prefix
                acc = 0
                for k, i in enumerate(cs):
                    acc += it[i].open
                    if acc == cash and k >= 1:
                        return [Hyp("party.prefix", 5, cs[:k + 1], note="prefix")]
                    if acc > cash:
                        break
                sw = self._sweep(line, p, cs, cash, ccy, cn_k)
                if sw is not None:
                    return [sw]
                cs = cs[:SUB_CANDS]
                pick = _subset_exact([it[i].open for i in cs], cash, SUB_PICKS, self)
                if pick is not None and len(pick) >= 2:
                    return [Hyp("party.subset", 5, [cs[k] for k in pick], note="subset")]
                cns = [j for j in self.book.open_of_party(p, cn_k) if it[j].ccy == ccy and self.eligible(line, j)][:6]
                if cns and len(cs) <= 20:
                    for k in range(1, min(3, len(cns)) + 1):
                        for combo in _combos(len(cns), k):
                            cn_sum = sum(it[cns[x]].open for x in combo)
                            pick = _subset_exact([it[i].open for i in cs], cash + cn_sum, 6, self)
                            if pick is not None:
                                return [Hyp("party.subset_cn", 5, [cs[x] for x in pick], [cns[x] for x in combo], note="subset_cn")]
        # withholding subset: one party, Σ(total - round(total*r)) == cash, gross rows
        for p in plist:
            wi = [i for i in per[p] if it[i].kind == "ARI" and it[i].open == it[i].total and it[i].ccy == line.ccy
                  and self._wht_rates(it[i])]
            if len(wi) < 2:
                continue
            wi = sorted(wi, key=lambda i: (it[i].due, it[i].issue, it[i].item_id))[:SUB_CANDS]
            cash = abs(line.cash)
            for r in self._wht_rates(it[wi[0]]):
                net = [it[i].total - (it[i].total * r + 5000) // 10000 for i in wi]
                pick = _subset_exact(net, cash, 10, self)
                if pick is not None and len(pick) >= 2:
                    return [Hyp("party.wht", 5, [wi[k] for k in pick], note="wht")]
        # single-item overpayment shape (surplus = round units or 1% / 10%); last resort
        ov = []
        for i in cands:
            x = it[i]
            c = self.cash_in(line, x.ccy)
            if x.kind == "ARI" and c and c[0] == c[1] and x.ccy == line.ccy and c[0] > x.open > 0 and x.open == x.total:
                sur = c[0] - x.open
                if any(abs(sur - u * 100) <= 1 for u in (1, 2, 5, 10, 50, 100)) or any(
                        abs(sur - x.open * p // 100) <= 1 or abs(sur - (x.open * p + 50) // 100) <= 1 for p in (1, 10)):
                    ov.append(i)
        if len(ov) == 1:
            return [Hyp("party.over", 4, ov, note="over")]
        trace["party"] = "AMOUNT_NO_FIT"
        return []

    def _sweep(self, line: Line, p: str, cs: list[int], cash: int, ccy: str, cn_k) -> Hyp | None:
        """Oldest-due prefix of the party's open invoices, netted with <=3 open credit notes,
        exact (zero gap) or with <=4 gaps inside the first k+4 invoices. Unique solution only."""
        it = self.items
        cns = [j for j in self.book.open_of_party(p, cn_k) if it[j].ccy == ccy and self.eligible(line, j)][:8]
        combos = [()]
        for k in range(1, min(3, len(cns)) + 1):
            combos += list(_combos(len(cns), k))
        pre = [0]
        for i in cs:
            pre.append(pre[-1] + it[i].open)
        sols = []
        for combo in combos:
            cn_sum = sum(it[cns[x]].open for x in combo)
            if not combo and True:
                continue                      # plain prefix handled by the caller
            tgt = cash + cn_sum
            for k in range(2, len(cs) + 1):
                if pre[k] == tgt:
                    sols.append((0, combo, cs[:k]))
                    break
                if pre[k] > tgt:
                    break
        if len(sols) == 1:
            _, combo, pick = sols[0]
            return Hyp("party.sweep_cn", 5, list(pick), [cns[x] for x in combo], note="sweep_cn")
        if sols:
            return None
        gap = []
        for combo in combos:
            cn_sum = sum(it[cns[x]].open for x in combo)
            tgt = cash + cn_sum
            k = next((k for k in range(1, len(cs) + 1) if pre[k] >= tgt), None)
            if k is None or k < 3:
                continue
            w = cs[:min(len(cs), k + 4, SUB_CANDS)]
            pick = _subset_exact([it[i].open for i in w], tgt, len(w), self)
            if pick is not None and len(pick) >= 3 and (len(w) - len(pick)) <= 4 and any(x >= k - 2 for x in pick):
                gap.append((combo, [w[x] for x in pick]))
        if len(gap) == 1:
            combo, pick = gap[0]
            if combo or True:
                return Hyp("party.sweep_gap", 5, pick, [cns[x] for x in combo], note="sweep_gap")
        return None

    def _wht_rates(self, x: Item) -> tuple[int, ...]:
        """bp withheld by this customer: learned per customer, else entity default, else ES01 Moroccan = 10 %."""
        r = self.wht_learned.get(x.party_id)
        if r is not None:
            return (r,) if r else ()
        if x.entity in WHT_RATES:
            return WHT_RATES[x.entity]
        if x.entity == "ES01" and (self.customers.get(x.party_id) or {}).get("country") == "MA":
            return (1000,)
        return ()

    def _piece_shape(self, line: Line, it: Item) -> bool:
        if it.kind != "ARI" or it.open != it.total:
            return False
        c = self.cash_in(line, it.ccy)
        if c is None or c[0] != c[1]:
            return False
        cash = c[0]
        if cash >= it.total:
            return False
        if self.adv_net.get(it.idx) == cash:
            return True
        for pct in (95, 90):
            if abs(cash * 100 - it.total * pct) <= 100:
                return True
        for n in range(2, 7):
            if it.total // n == cash:
                return True
        return False

    def _short_shape(self, line: Line, it: Item) -> bool:
        if it.kind != "ARI" or it.open != it.total:
            return False
        c = self.cash_in(line, it.ccy)
        if c is None or c[0] != c[1]:
            return False
        cash = c[0]
        short = it.total - cash
        if short <= 0:
            return False
        t = self.terms.get(it.terms)
        if t and t[1] > 0:
            disc = (it.total * t[1] + 5000) // 10000
            if abs(short - disc) <= 1 and line.day <= it.issue + t[2] + 3:
                return True
        if it.entity in ("BR01", "MX01", "MA01", "ES01"):
            for bp in WHT_BP:
                if abs(short - (it.total * bp + 5000) // 10000) <= 1:
                    return True
        cust = self.customers.get(it.party_id) or {}
        ctry = cust.get("country")
        if ctry and ctry != self.entity_country.get(it.entity) and short <= 7110 and short * 1000 <= it.total * 32:
            return True
        return False

    def _oldest(self, idxs: list[int]) -> int:
        it = self.items
        return min(idxs, key=lambda i: (it[i].due, it[i].issue, it[i].item_id))

    def _month_pick(self, line: Line, idxs: list[int]) -> int | None:
        m = MONTH_RE.search(line.t_join or "")
        if not m:
            return None
        mon = MONTHS[m.group(1)]
        yr = m.group(2)
        y = (2000 + int(yr) if yr and len(yr) == 2 else int(yr)) if yr else date.fromordinal(line.day).year
        ny, nm = (y + 1, 1) if mon == 12 else (y, mon + 1)
        hit = [i for i in idxs if (lambda d: (d.year, d.month))(date.fromordinal(self.items[i].issue)) == (ny, nm)]
        return self._oldest(hit) if hit else None


# ---------------------------------------------------------------------- arbitration
def fit_level(res: Resolver, line: Line, h: Hyp) -> int:
    it = res.items
    if not h.items:
        return 0
    if h.resolver.startswith(("ops", "advice", "return")) or h.is_return:
        return 3
    ccy = it[h.items[0]].ccy
    s = sum(it[i].open for i in h.items) - sum(it[i].open for i in h.cn)
    f = res.fits_sum(line, ccy, s)
    if f:
        return f
    if h.note == "dup":
        return 2
    return 1


def arbitrate(res: Resolver, line: Line, hyps: list[Hyp]) -> Hyp | None:
    hyps = [h for h in hyps if h and h.items]
    if not hyps:
        return None
    it = res.items

    def key(h: Hyp):
        return (-fit_level(res, line, h), h.tier, len(h.items),
                min((it[i].due for i in h.items), default=0), tuple(it[i].item_id for i in h.items))
    return min(hyps, key=key)


# ---------------------------------------------------------------------- helpers
def _alnum(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _combos(n: int, k: int):
    from itertools import combinations
    return combinations(range(n), k)


def _subset_exact(vals: list[int], target: int, max_picks: int, res: Resolver | None = None) -> list[int] | None:
    """Indices of a subset of vals summing exactly to target (smallest, earliest), bounded."""
    if target <= 0 or not vals:
        return None
    if res is not None:
        res.line_calls = getattr(res, "line_calls", 0) + 1
        if res.line_calls > LINE_SUBSET_CALLS:
            res.capped = getattr(res, "capped", 0) + 1
            return None
    vals = vals[:SUB_CANDS]
    if target <= BITSET_MAX:
        # reachability as a big-int bitset (one shift per value), backtrack over prefixes
        mask = (1 << (target + 1)) - 1
        pre = [1]
        for v in vals:
            r = pre[-1]
            pre.append((r | (r << v)) & mask if v > 0 else r)
        if res is not None:
            res.states += len(vals)
        if not (pre[-1] >> target) & 1:
            return None
        picks, t = [], target
        for k in range(len(vals) - 1, -1, -1):
            if (pre[k] >> t) & 1:
                continue          # reachable without item k: prefer the earlier items
            picks.append(k)
            t -= vals[k]
        if t != 0 or len(picks) > max_picks:
            return None
        return sorted(picks)
    # DP over reachable sums: sum -> tuple(indices)
    reach: dict[int, tuple[int, ...]] = {0: ()}
    states = 0
    for k, v in enumerate(vals):
        if v <= 0:
            continue
        new = {}
        for s, picks in reach.items():
            t = s + v
            if t > target or len(picks) >= max_picks:
                continue
            if t not in reach and t not in new:
                new[t] = picks + (k,)
                if t == target:
                    if res is not None:
                        res.states += states
                    return list(new[t])
        reach.update(new)
        states += len(reach)
        if states > SUB_STATES:
            if res is not None:
                res.states += states
                res.capped = getattr(res, "capped", 0) + 1
            return None
    if res is not None:
        res.states += states
    return None
