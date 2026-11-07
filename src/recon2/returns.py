"""Returns (T1): a line that reverses an earlier ops operation -> RETURNED on its items.

DD return (mandate / batch ref), cheque returned (cheque number), card chargeback
(PAN4 + MID, ±1 month of settlement files), AP rejected transfer (e2e incl. netted APC).
represent(): re-payment of an item a return re-opened, within 35 days.
"""

from __future__ import annotations

import re

from .model import Hyp, Line
from .ops import Ops, fold, line_text, tokens_of

DD_WORDS = re.compile(r"DEVOL|RUECKLASTSCHRIFT|IMPAYE|RETURN|RECHAZ|UNPAID|RETOUR|\bR\d{2}\b|\b(?:AM|MD|MS|AC|SL)\d{2}\b")
EXCLUDE = re.compile(r"ANULACION OPERACION INDEBIDA|RET\.? GARANTIA|RETENTION RELEASE|LIBERACION RETENCION|"
                     r"FREIGABE EINBEHALT|DEVOLUCION PRESTAMO|PRESTAMO")
MANDATE = re.compile(r"\b([A-Z]{2,4}-[A-Z]{2}-\d{5,8})\b")
BATCH_REF = re.compile(r"(?:DEV|REM ?|REMESA ?|EINZUG ?|REMISE ?)(\d{11})\b")
CHEQUE = re.compile(r"(?:RETURNED ITEM CHK|CHEQUE IMPAYE|CHQ IMPAYE|CHEQUE DEVUELTO|SCHECK)\D{0,5}(\d{6,8})")
CHARGEBACK = re.compile(r"CHARGEBACK|CONTRACARGO|RETROFACTURATION|IMPAYE CB|RUECKBELASTUNG")
PAN = re.compile(r"\*{2,}\s?(\d{4})")
MID = re.compile(r"(?<!\d)(\d{9})(?!\d)")
AP_RET = re.compile(r"DEVOLUCION TRANSFERENCIA|RETURN|RETOUR|RUECKUEBERWEISUNG|RECHAZ|REJECT|AC04|AC01|MD07")
REPRESENT_DAYS = 35


class Returns:
    def __init__(self, phase, ops: Ops):
        self.phase, self.ops = phase, ops
        self.reopened: list[tuple[int, int]] = []   # (day, item idx) proposed as RETURNED

    def resolve(self, line: Line, book) -> Hyp | None:
        text = line_text(line)
        if EXCLUDE.search(text):
            return None
        cash = abs(line.cash)
        hyp = None
        if line.cash < 0:
            if CHARGEBACK.search(text):
                hyp = self._card(line, text, cash, book)
            elif CHEQUE.search(text):
                hyp = self._cheque(line, text, cash, book)
            elif DD_WORDS.search(text):
                hyp = self._dd(line, text, cash, book)
        elif line.cash > 0 and AP_RET.search(text):
            hyp = self._ap(line, text, cash, book)
        if hyp is not None:
            for i in hyp.items + hyp.cn:
                self.reopened.append((line.day, i))
        return hyp

    # ---------------------------------------------------------------- per kind
    def _mk(self, kind: str, op, rows, line, book) -> Hyp | None:
        items, cn, used = [], [], set()
        for r in rows:
            i = self.ops.row_item(op, r, line, book, used, closed_ok=True)
            if i is None:
                continue
            used.add(i)
            (cn if op.kind == "run" and r.amt < 0 else items).append(i)
        if not items and not cn:
            return None
        return Hyp(resolver=f"ret.{kind}", tier=1, items=items, cn=cn, is_return=True,
                   advice={i: {"paid": book.items[i].total, "ded": 0, "reason": "", "src": "ret"}
                           for i in items + cn}, note=f"{kind}:{op.oid}")

    def _dd(self, line, text, cash, book) -> Hyp | None:
        mandates = sorted(set(MANDATE.findall(text)))
        batch = BATCH_REF.search(text)
        bid = batch.group(1) if batch else ""
        cands = []
        for mnd in mandates:
            for op, i in self.ops.dd_by_mandate.get(mnd, ()):
                if op.day <= line.day and (not bid or op.oid == bid):
                    cands.append((op, mnd))
        if not cands and bid:   # no mandate printed: debtor rows of that batch by amount
            op = self.ops.ops.get(("dd", bid))
            if op:
                rows = [r for r in op.rows if r.amt == cash]
                if len(rows) == 1:
                    return self._mk("dd", op, rows, line, book)
        if not cands:
            return None
        cands.sort(key=lambda c: (-c[0].day, c[0].oid))
        for op, mnd in cands:        # latest collection <= day whose amount explains the cash
            rows = [r for r in op.rows if r.party.get("mandate") == mnd]
            if sum(r.amt for r in rows) != cash:
                rows = [r for r in rows if r.amt == cash][:1]
            if rows:
                return self._mk("dd", op, rows, line, book)
        return None

    def _cheque(self, line, text, cash, book) -> Hyp | None:
        m = CHEQUE.search(text)
        hits = self.ops.chq_by_number.get(m.group(1).lstrip("0"), [])
        hits = sorted((h for h in hits if h[0].day <= line.day), key=lambda h: (-h[0].day, h[0].oid))
        for op, i in hits:
            return self._mk("cheque", op, [op.rows[i]], line, book)
        return None

    def _card(self, line, text, cash, book) -> Hyp | None:
        raw = " ".join(x for x in (line.text, line.ref1, line.ref2, line.rinfo) if x)
        pm = PAN.search(raw) or re.search(r"(?:TARJ|CARD|CB|KARTE)\D{0,6}(\d{4})\b", text)
        text = text + " " + raw
        if not pm:
            return None
        pan = pm.group(1)
        best = []
        for mid in sorted(set(MID.findall(text))):
            for op in self.ops.cards_by_mid.get(mid, ()):
                if not (line.day - 62 <= op.day <= line.day):
                    continue
                for r in op.rows:
                    if r.party.get("card") == pan:
                        exact = 0 if r.gross == cash else 1 if r.net == cash else 2
                        best.append((exact, line.day - op.day, op.oid, op, r))
        best.sort(key=lambda b: b[:3])
        if not best:
            return None
        if best[0][0] == 2 and len(best) > 1:
            return None
        return self._mk("card", best[0][3], [best[0][4]], line, book)

    def _ap(self, line, text, cash, book) -> Hyp | None:
        for op, grp in self.ops.find(line, text):
            if op.kind != "run" or grp is None:
                continue
            rows = [r for r in op.rows if r.group == grp]
            return self._mk("ap", op, rows, line, book)
        return None

    # ---------------------------------------------------------------- re-presentation
    def represent(self, line: Line, book, parties: list[str]) -> Hyp | None:
        if line.cash <= 0 or not self.reopened:
            return None
        cash = line.cash
        text = line_text(line)
        toks = tokens_of(text)
        pset = set(parties)
        cands = []
        for d, i in sorted(self.reopened, key=lambda x: (-x[0], x[1])):
            if not (0 <= line.day - d <= REPRESENT_DAYS):
                continue
            it = book.items[i]
            if it.kind != "ARI" or it.open != cash:
                continue
            hist = getattr(book, "hist", {}).get(i) or []
            if hist and hist[-1][2] != "RETURNED":
                continue
            named = any(len(t) >= 4 and fold(it.doc).replace("-", "").endswith(t.replace("-", "")[-6:])
                        for t in toks if any(c.isdigit() for c in t))
            if it.party_id in pset or named:
                cands.append((0 if named else 1, 0 if it.party_id in pset else 1, -d, it.item_id, i))
        if not cands:
            return None
        cands.sort()
        return Hyp(resolver="ret.represent", tier=1, items=[cands[0][-1]], note="represent")
