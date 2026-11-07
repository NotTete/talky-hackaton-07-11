"""Stateful, chronological item book (CLAUDE.md §7) with indexes built once (§11)."""

from __future__ import annotations

from bisect import bisect_left, insort

from recon2.model import Alloc, Item, Line

SCAN = 64            # unscoped prefix/suffix window; overflow -> ambiguous -> []
SCAN_SCOPED = 4096   # scoped window (filtered by party)
CLOSE = {"", "OVERPAYMENT", "FX_DIFFERENCE", "SHORT_DISCOUNT", "SHORT_WITHHOLDING", "SHORT_BANK_CHARGES"}
REDUCE = {"PARTIAL", "SHORT_DISPUTE"}


def alnum(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def doc_variants(doc: str) -> list[str]:
    a = alnum(doc)
    if not a:
        return []
    out = [a]
    digits = "".join(ch for ch in a if ch.isdigit())
    if len(digits) >= 5:
        out.append(digits)
        nz = digits.lstrip("0")
        if len(nz) >= 5:
            out.append(nz)
    return list(dict.fromkeys(out))


class Book:
    def __init__(self, items: list[Item], masters=None):
        self.items = items
        self.masters = masters
        self.by_id = {it.item_id: it.idx for it in items}
        self.hist: dict[int, list[tuple[int, int, str, int]]] = {}
        self.closed_on: dict[int, int] = {}
        doc: dict[str, list[int]] = {}
        pdoc: dict[str, list[int]] = {}
        party: dict[str, list[int]] = {}
        amount: dict[tuple[str, str, int], list[int]] = {}
        for it in items:
            i = it.idx
            for v in doc_variants(it.doc):
                doc.setdefault(v, []).append(i)
            a = alnum(it.doc)
            if a and it.kind in ("API", "APC"):
                pdoc.setdefault(f"{it.party_id}|{a}", []).append(i)
            p = alnum(it.po)
            if len(p) >= 6:
                doc.setdefault(p, []).append(i)
            if it.party_id:
                party.setdefault(it.party_id, []).append(i)
            if it.open > 0:
                amount.setdefault((it.entity, it.ccy, it.open), []).append(i)
        self._doc = doc
        self._pdoc = pdoc
        self._amount = amount
        # prefix/suffix keys: alnum docs + POs (digit-core variants are redundant for suffix)
        keys = {alnum(it.doc) for it in items} | {alnum(it.po) for it in items if len(alnum(it.po)) >= 6}
        keys.discard("")
        self._keys = sorted(keys)
        self._rkeys = sorted(k[::-1] for k in keys)
        for p, lst in party.items():
            lst.sort(key=self._party_key)
        self._party = party
        self._gone: set[int] = set()  # removed from party lists by lazy deletion

    # ---------------------------------------------------------------- lookups
    def _party_key(self, i: int):
        it = self.items[i]
        return (it.due, it.issue, it.doc, i)

    def by_doc(self, token: str) -> list[int]:
        """Exact match over doc variants (alnum, digit core, no leading zeros, PO).

        'SUPPLIER|DOC' looks up the party-qualified AP index."""
        if "|" in token:
            p, d = token.split("|", 1)
            return sorted(set(self._pdoc.get(f"{p}|{alnum(d)}", ())))
        a = alnum(token)
        if not a:
            return []
        out = set(self._doc.get(a, ()))
        if a.isdigit():
            nz = a.lstrip("0")
            if len(nz) >= 5 and nz != a:
                out.update(self._doc.get(nz, ()))
        return sorted(out)

    def _expand(self, keys: list[str], scope: set[str] | None) -> list[int]:
        out: set[int] = set()
        for k in keys:
            for i in self._doc.get(k, ()):
                if scope is None or self.items[i].party_id in scope:
                    out.add(i)
        return sorted(out)

    def _scan(self, arr: list[str], tok: str, scope, rev: bool) -> list[int]:
        cap = SCAN if scope is None else SCAN_SCOPED
        j = bisect_left(arr, tok)
        hits = []
        while j < len(arr) and arr[j].startswith(tok):
            hits.append(arr[j][::-1] if rev else arr[j])
            j += 1
            if len(hits) > cap:
                return []
        return self._expand(hits, scope)

    def by_doc_suffix(self, token: str, scope: set[str] | None = None) -> list[int]:
        a = alnum(token)
        if len(a) < 6:
            return []
        return self._scan(self._rkeys, a[::-1], scope, True)

    def by_doc_prefix(self, token: str, scope: set[str] | None = None) -> list[int]:
        a = alnum(token)
        if len(a) < 7:
            return []
        return self._scan(self._keys, a, scope, False)

    def open_of_party(self, party: str, kinds: tuple[str, ...]) -> list[int]:
        lst = self._party.get(party)
        if not lst:
            return []
        items = self.items
        if any(items[i].open <= 0 for i in lst):  # lazy deletion: compact closed items
            keep = []
            for i in lst:
                if items[i].open > 0:
                    keep.append(i)
                else:
                    self._gone.add(i)
            lst[:] = keep
        return [i for i in lst if items[i].kind in kinds]

    def open_by_amount(self, entity: str, ccy: str, cents: int) -> list[int]:
        return list(self._amount.get((entity, ccy, cents), ()))

    def settled(self, idx: int) -> bool:
        return self.items[idx].open == 0 and bool(self.hist.get(idx))

    # ---------------------------------------------------------------- state
    def _set_open(self, it: Item, new: int) -> None:
        old = it.open
        if new == old:
            return
        if old > 0:
            k = (it.entity, it.ccy, old)
            lst = self._amount.get(k)
            if lst is not None and it.idx in lst:
                lst.remove(it.idx)
                if not lst:
                    del self._amount[k]
        it.open = new
        if new > 0:
            insort(self._amount.setdefault((it.entity, it.ccy, new), []), it.idx)
            if it.idx in self._gone:  # reopened (RETURNED): back into the party list
                self._gone.discard(it.idx)
                insort(self._party.setdefault(it.party_id, []), it.idx, key=self._party_key)

    def apply(self, line: Line, allocs: list[Alloc]) -> None:
        for a in allocs:
            it = self.items[a.item]
            if a.exc == "RETURNED":
                signed = -a.amount
                self._set_open(it, min(it.total, it.open + a.amount))
                self.closed_on.pop(it.idx, None)
            elif a.exc == "DUPLICATE_PAYMENT":
                signed = a.amount
            elif a.exc in REDUCE:
                signed = a.amount
                self._set_open(it, max(0, it.open - a.amount))
            else:  # CLOSE set: cleared (discount/withholding/charges/fx/overpayment absorbed)
                signed = a.amount
                self._set_open(it, 0)
            self.hist.setdefault(it.idx, []).append((line.idx, signed, a.exc, line.day))
            if it.open == 0:
                self.closed_on.setdefault(it.idx, line.day)
            it.residue = 0 < it.open < it.total
