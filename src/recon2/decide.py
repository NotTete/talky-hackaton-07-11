"""WP-D accounting: split cash over a hypothesis, then decide per-row amount + exception.

Pure functions (DESIGN §4).  Depends only on:
  * Item fields (model.Item),
  * book.items[idx] and book.hist.get(idx) -> [(line_idx, signed applied, exc, day)],
  * ctx.masters.customers[id]['country'], .entity_country, .terms[code] = (days, pct_bp, disc_days),
    .fx.convert(cents, src, dst, day) (optional),
  * ctx.wht_rate (customer -> bp; 0 = known non-withholder), ctx.plans (item idx -> n instalments).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from recon2.model import Alloc, Hyp, Item, Line

DISPUTE_REASONS = ("COOP", "PRICE", "LOGISTIC", "SHORT DELIVERY", "PENAL", "RETRASO", "DAMAGED")
NOT_DISPUTE = ("DISCOUNT", "DESCUENTO", "DTO", "SKONTO", "ESCOMPTE", "PRONTO PAGO", "EARLY")
WHT_DEFAULTS = {"BR01": (150, 1000), "MA01": (150, 1000), "MX01": (125, 1000)}
DUP_WINDOW = {"AR": 15, "AP": 50}
FX_BAND_BP = 300          # cross-ccy: converted cash within 3% of open -> fully cleared
SBC_MAX = 7110            # cents
LATE_MULTI = 7             # extra days for discount rows on multi-invoice lines (SD median 7, PARTIAL >=17)
SBC_MAX_BP = 320


@dataclass
class Ctx:
    masters: object
    wht_rate: dict[str, int] = field(default_factory=dict)
    plans: dict[int, int] = field(default_factory=dict)   # item idx -> instalment count (PDF)


# ---------------------------------------------------------------- helpers
def _half_up(num: int, den: int) -> int:
    return (2 * num + den) // (2 * den)


def _pct(total: int, bp: int) -> int:
    return _half_up(total * bp, 10000)


def _hist(book, idx: int) -> list:
    return book.hist.get(idx) or []


def _first(book, it: Item) -> bool:
    """Untouched: full balance open and never paid (carried part-paid items are not first)."""
    return it.open == it.total and not any(h[1] > 0 for h in _hist(book, it.idx))


def _to_ccy(line: Line, amt: int, ccy: str, ctx: Ctx) -> int | None:
    if ccy == line.ccy:
        return amt
    if line.orig and line.orig_ccy == ccy and amt == abs(line.cash):
        return line.orig
    fx = getattr(ctx.masters, "fx", None)
    if fx is None:
        return None
    try:
        return fx.convert(amt, line.ccy, ccy, line.day)
    except Exception:
        return None


def _disc(it: Item, line: Line, ctx: Ctx, book, late: int = 0) -> int:
    """Discount in cents if the row is discount-eligible (ARI, pct terms, untouched, in window) else 0.
    `late` widens the window (multi-invoice lines only: 903/60,895 SD rows are 4..34 days late)."""
    if it.kind != "ARI" or not it.terms:
        return 0
    t = ctx.masters.terms.get(it.terms)
    if not t or not t[1] or line.day > it.issue + t[2] + 3 + late or not _first(book, it):
        return 0
    return _pct(it.total, t[1])


def _wht_rates(it: Item, ctx: Ctx) -> tuple[int, ...]:
    if it.kind != "ARI":
        return ()
    r = ctx.wht_rate.get(it.party_id)
    if r is not None:
        return (r,) if r else ()
    if it.entity in WHT_DEFAULTS:
        return WHT_DEFAULTS[it.entity]
    if it.entity == "ES01":
        c = ctx.masters.customers.get(it.party_id) or {}
        if c.get("country") == "MA":
            return (1000,)
    return ()


def _line_target(line: Line, hyp: Hyp, book, ctx: Ctx):
    """(|cash| in item ccy, item ccy, ΣCN open) or None when items span currencies / no rate."""
    its = [book.items[i] for i in hyp.items]
    cns = [book.items[i] for i in hyp.cn]
    ccys = {x.ccy for x in its + cns}
    if len(ccys) != 1:
        return None
    ccy = ccys.pop()
    x = _to_ccy(line, abs(line.cash), ccy, ctx)
    if x is None:
        return None
    return x, ccy, sum(c.open for c in cns)


def _line_shorts(line: Line, hyp: Hyp, book, ctx: Ctx) -> tuple[str, dict[int, int]]:
    """Line-level E7/E8 identity (same ccy only): cash == Σ(total − ded on eligible) + Σ other open − ΣCN.
    Returns ('SHORT_DISCOUNT'|'SHORT_WITHHOLDING'|'', {item idx: deduction})."""
    tg = _line_target(line, hyp, book, ctx)
    if tg is None or tg[1] != line.ccy:
        return "", {}
    x, _, cn = tg
    its = [book.items[i] for i in hyp.items]
    base = sum(it.open for it in its) - cn
    tol = 1  # ±1c per deducted row (rounding)
    d = {it.idx: _disc(it, line, ctx, book) for it in its}
    d = {k: v for k, v in d.items() if v}
    if d and abs(base - sum(d.values()) - x) <= tol * len(d):
        return "SHORT_DISCOUNT", d
    if len(its) > 1:   # late rows on a multi-invoice line: in-window rows + the subset of late ones that fits
        extra = [(it.idx, _disc(it, line, ctx, book, LATE_MULTI)) for it in its if it.idx not in d]
        extra = [e for e in extra if e[1]][:14]
        need = base - sum(d.values()) - x
        for mask in range(1, 1 << len(extra)):
            pick = [extra[j] for j in range(len(extra)) if mask >> j & 1]
            if abs(need - sum(v for _, v in pick)) <= len(d) + len(pick):
                return "SHORT_DISCOUNT", {**d, **dict(pick)}
    # withholding: one rate per customer; try the candidate rates jointly (all rows share entity mostly)
    cands = sorted({r for it in its for r in _wht_rates(it, ctx)})
    for r in cands:
        w = {it.idx: _pct(it.total, r) for it in its if r in _wht_rates(it, ctx) and _first(book, it)}
        if w and abs(base - sum(w.values()) - x) <= tol * len(w):
            return "SHORT_WITHHOLDING", w
    return "", {}


# ---------------------------------------------------------------- split
def split_cash(line: Line, hyp: Hyp, book, ctx: Ctx) -> dict[int, int] | None:
    """Allocate |cash| over hyp.items (+cn) in item ccy; None if the arithmetic does not fit."""
    if not hyp.items and not hyp.cn:
        return None
    if not hyp.items:                     # CN alone (supplier refund / CN paid out)
        hyp_items, cns = list(hyp.cn), []
    else:
        hyp_items, cns = list(hyp.items), list(hyp.cn)
    items = [book.items[i] for i in hyp_items]
    out: dict[int, int] = {c: book.items[c].open for c in cns}

    if hyp.is_return:
        for it in items:
            out[it.idx] = _reopened(book, it)
        for c in cns:
            out[c] = _reopened(book, book.items[c])
        return out
    if hyp.resolver.startswith("ops.card"):
        for it in items:
            out[it.idx] = it.open or it.total
        return out
    if hyp.advice:
        ok = True
        for it in items:
            a = hyp.advice.get(it.idx)
            if a is None or a.get("paid") is None:
                ok = False
                break
            out[it.idx] = abs(a["paid"])
        if ok:
            return out
        out = {c: book.items[c].open for c in cns}

    h = Hyp(hyp.resolver, hyp.tier, hyp_items, cns)
    tg = _line_target(line, h, book, ctx)
    if tg is None:   # items in several currencies / no rate: the resolver must split per currency
        return None
    x, ccy, cn = tg
    t = x + cn                            # what the invoices received
    s = sum(it.open for it in items)
    cross = ccy != line.ccy
    if t <= 0:
        return None
    if t == s or (cross and abs(t - s) * 10000 <= FX_BAND_BP * s):
        for it in items:
            out[it.idx] = it.open
        return out
    closed = [it for it in items if it.open <= 0]
    if len(items) == 1:
        out[items[0].idx] = t
        return out
    if not cross:
        kind, ded = _line_shorts(line, h, book, ctx)
        if kind:
            for it in items:
                out[it.idx] = it.open - ded.get(it.idx, 0)
            return out
    if closed:
        return None
    if not cross:   # late discount taken on every discount-terms row (ar_discount_late: each row PARTIAL)
        dl = {it.idx: _disc(it, line, ctx, book, 10**6) for it in items}
        if any(dl.values()) and abs(s - sum(dl.values()) - t) <= sum(1 for v in dl.values() if v):
            for it in items:
                out[it.idx] = it.open - dl[it.idx]
            return out
        # late discount on a SUBSET of the discount rows: a unique exact combination is evidence
        cand = [(i, v) for i, v in dl.items() if v]
        if 1 <= len(cand) <= 40:
            gap = s - t
            hits = []
            for k in (1, 2, 3):
                for comb in itertools.combinations(cand, k):
                    if abs(sum(v for _, v in comb) - gap) <= k:
                        hits.append(comb)
            if len(hits) == 1:
                for it in items:
                    out[it.idx] = it.open
                for i, v in hits[0]:
                    out[i] = book.items[i].open - v
                return out
    p = hyp.partial_item if hyp.partial_item in hyp_items else hyp_items[-1]
    # residual to the single partial item; in-window discounts on the full rows are taken first
    for use_disc in ((True, False) if not cross else (False,)):
        full = {it.idx: it.open - (_disc(it, line, ctx, book) if use_disc else 0) for it in items if it.idx != p}
        r = t - sum(full.values())
        if 0 < r < book.items[p].open:
            out.update(full)
            out[p] = r
            return out
    return None


def _reopened(book, it: Item) -> int:
    for h in reversed(_hist(book, it.idx)):
        if h[1] > 0:
            return h[1]
    return it.total


# ---------------------------------------------------------------- decide
def _plan(book, it: Item, ctx: Ctx) -> tuple[int, int] | None:
    """(piece, last) of a known instalment plan: PDF n, else inferred from the first prior piece."""
    n = ctx.plans.get(it.idx)
    if not n:
        prior = [h[1] for h in _hist(book, it.idx) if h[1] > 0]
        if not prior:
            return None
        p1 = prior[0]
        n = _half_up(it.total, p1) if p1 else 0
        if not 2 <= n <= 6:
            return None
        piece = it.total // n
        last = it.total - (n - 1) * piece
        if abs(p1 - piece) > 1 and abs(p1 - last) > 1:
            return None
        return piece, last
    piece = it.total // n
    return piece, it.total - (n - 1) * piece


def _retention_first(book, it: Item, a: int) -> bool:
    """95%/90% main payment of a retention invoice (even if the release was booked first)."""
    for bp in (9500, 9000):
        if abs(a - _pct(it.total, bp)) <= 1:
            paid = sum(h[1] for h in _hist(book, it.idx) if h[1] > 0)
            if paid == 0 or abs(paid - (it.total - a)) <= 1:
                return True
    return False


def _over_shape(sur: int, ob: int) -> bool:
    """OVERPAYMENT surplus is a round number of units or 1% / 10% of the open amount."""
    if any(abs(sur - u * 100) <= 1 for u in (1, 2, 5, 10, 50, 100)):
        return True
    return any(abs(sur - ob * p // 100) <= 1 or abs(sur - (ob * p + 50) // 100) <= 1 for p in (1, 10))


def decide(line: Line, hyp: Hyp, book, ctx: Ctx, applied: dict[int, int]) -> list[Alloc]:
    if hyp.items:
        inv, cns = list(hyp.items), list(hyp.cn)
    else:
        inv, cns = list(hyp.cn), []
    n_inv = len(inv)
    single = n_inv == 1 and not cns
    out: list[Alloc] = []

    if hyp.is_return:
        for i in inv + cns:
            a = applied.get(i) or _reopened(book, book.items[i])
            out.append(Alloc(i, a, "RETURNED", "E1"))
        return out

    line_kind, ded = ("", {})
    if not hyp.resolver.startswith("ops.card") and not hyp.advice:
        line_kind, ded = _line_shorts(line, hyp, book, ctx)

    for i in inv:
        it = book.items[i]
        a = applied.get(i, 0)
        cross = it.ccy != line.ccy
        ob = it.open
        hist = _hist(book, i)

        if hyp.resolver.startswith("ops.card"):                          # E0
            out.append(Alloc(i, ob if ob > 0 else it.total, "", "E0"))
            continue
        if it.kind in ("ARC", "APC"):                                    # CN on its own line
            out.append(Alloc(i, ob if ob > 0 else a, "", "E3"))
            continue
        if ob <= 0 and single and any(h[1] > 0 for h in hist):           # E2
            out.append(Alloc(i, a or _reopened(book, it), "DUPLICATE_PAYMENT", "E2"))
            continue
        if (single and it.kind == "API" and 0 < ob < a and hist and hist[-1][1] > 0
                and abs(hist[-1][1] - a) <= 1 and line.day - hist[-1][3] <= DUP_WINDOW["AP"]):
            out.append(Alloc(i, a, "DUPLICATE_PAYMENT", "E2p"))                # AP dup of a partial piece
            continue
        adv = (hyp.advice or {}).get(i)
        if adv:                                                          # E4
            reason = (adv.get("reason") or "").upper()
            # Organiser examples: ANY stated deduction that is not a discount or withholding is a dispute.
            _ded = abs(adv.get("ded") or 0) or max(0, abs(adv.get("gross") or 0) - abs(adv.get("paid") or 0))
            if (reason and it.kind == "ARI" and reason.strip() != "RET" and not any(k in reason for k in NOT_DISPUTE)
                    and (any(k in reason for k in DISPUTE_REASONS) or _ded > 0)):
                dispute_done = True
                out.append(Alloc(i, abs(adv.get("paid") or a), "SHORT_DISPUTE", "E4"))
                continue
            if reason.strip() == "RET" and it.kind == "ARI":
                exc = "FX_DIFFERENCE" if cross else "SHORT_WITHHOLDING"
                out.append(Alloc(i, abs(adv.get("gross") or it.total), exc, "E4r"))
                continue
            d = _disc(it, line, ctx, book)
            if d and abs(abs(adv.get("ded") or 0) - d) <= 1:
                out.append(Alloc(i, ob, "FX_DIFFERENCE" if cross else "SHORT_DISCOUNT", "E7a"))
                continue
        if i in ded and line_kind:                                       # E7 / E8 (line-level)
            exc = "FX_DIFFERENCE" if cross else line_kind
            out.append(Alloc(i, ob, exc, "E7" if line_kind == "SHORT_DISCOUNT" else "E8"))
            continue
        if not adv and not cross and i != hyp.partial_item and n_inv > 1:  # E7 per row (split took the discount)
            d = _disc(it, line, ctx, book)
            if d and abs(a - (it.total - d)) <= 1:
                out.append(Alloc(i, ob, "SHORT_DISCOUNT", "E7r"))
                continue
        plan = _plan(book, it, ctx) if it.kind in ("ARI", "API") else None
        if plan:                                                         # E6 (before E5)
            piece, last = plan
            npaid = sum(1 for h in hist if h[1] > 0)
            is_last = abs(a - last) <= 1 and (abs(piece - last) > 1 or npaid + 1 >= _half_up(it.total, piece))
            if is_last:
                out.append(Alloc(i, a if a < ob else ob, "FX_DIFFERENCE" if cross and a >= ob else "", "E6"))
                continue
            if abs(a - piece) <= 1:
                out.append(Alloc(i, a, "PARTIAL", "E6"))
                continue
        if it.kind in ("ARI", "API") and _retention_first(book, it, a) and not cross:
            out.append(Alloc(i, a, "PARTIAL", "E6r"))
            continue
        cleared = a >= ob or (cross and (ob - a) * 10000 <= FX_BAND_BP * ob)
        if cleared:                                                      # E5
            if cross:
                out.append(Alloc(i, ob, "FX_DIFFERENCE", "E5"))
            elif it.kind == "ARI" and single and a > ob and _first(book, it) and _over_shape(a - ob, ob):
                out.append(Alloc(i, ob, "OVERPAYMENT", "E5"))
            else:
                out.append(Alloc(i, ob, "", "E5"))
            continue
        short = ob - a
        if single and it.kind == "ARI" and not cross and _first(book, it):
            # 1-item E7/E8 already covered by _line_shorts; E9 bank charges
            c = ctx.masters.customers.get(it.party_id) or {}
            ec = ctx.masters.entity_country.get(it.entity)
            disc_shaped = any(abs(short - _pct(it.total, bp)) <= 1 for bp in (100, 200, 300))
            if (c.get("country") and ec and c.get("country") != ec and 0 < short <= SBC_MAX
                    and short * 10000 <= SBC_MAX_BP * it.total and not disc_shaped):
                out.append(Alloc(i, ob, "SHORT_BANK_CHARGES", "E9"))
                continue
        if a <= 0:
            continue
        out.append(Alloc(i, a, "PARTIAL", "E10"))                        # E10

    for c in cns:                                                        # E3 netted CN
        it = book.items[c]
        out.append(Alloc(c, it.open if it.open > 0 else (applied.get(c) or it.total), "", "E3"))
    return out
