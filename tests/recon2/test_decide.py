from types import SimpleNamespace

from recon2.decide import Ctx, decide, split_cash
from recon2.model import Hyp, Item, Line

D0 = 738000


def _ms():
    return SimpleNamespace(customers={"C1": {"country": "US"}, "C2": {"country": "CA"}, "C3": {"country": "BR"}},
                           entity_country={"US01": "US", "BR01": "BR", "ES01": "ES"},
                           terms={"D2N30": (30, 200, 10), "T30": (30, 0, 0)}, fx=None)


class B:
    def __init__(self, items, hist=None):
        self.items = items
        self.hist = hist or {}


def _it(idx, total, kind="ARI", ent="US01", party="C1", terms="T30", open_=None, issue=D0, ccy="EUR"):
    return Item(idx, f"{kind}-2025-{idx:07d}", kind, ent, party, "", f"D{idx}", issue, issue + 30, ccy, total,
                total if open_ is None else open_, terms)


def _line(cash, d=D0 + 5, ccy="EUR"):
    return Line(0, "BT", "", "", "", "", d, d, cash, ccy, None, "", "", "", "", "", "", "", "", "", "")


def run(line, hyp, book, ctx=None):
    ctx = ctx or Ctx(_ms())
    ap = split_cash(line, hyp, book, ctx)
    assert ap is not None
    return [(a.item, a.amount, a.exc) for a in decide(line, hyp, book, ctx, ap)]


def test_discount_line_level():
    b = B([_it(0, 10000, terms="D2N30"), _it(1, 5000, terms="D2N30"), _it(2, 3000)])
    assert run(_line(9800 + 4900 + 3000), Hyp("t", 2, [0, 1, 2]), b) == [
        (0, 10000, "SHORT_DISCOUNT"), (1, 5000, "SHORT_DISCOUNT"), (2, 3000, "")]


def test_late_discount_partial():
    b = B([_it(0, 10000, terms="D2N30")])
    assert run(_line(9800, d=D0 + 20), Hyp("t", 2, [0]), b) == [(0, 9800, "PARTIAL")]


def test_overpayment_and_bank_charges():
    b = B([_it(0, 10000)])
    assert run(_line(10100), Hyp("t", 2, [0]), b) == [(0, 10000, "OVERPAYMENT")]
    b = B([_it(0, 100000, party="C2")])
    assert run(_line(100000 - 2500), Hyp("t", 2, [0]), b) == [(0, 100000, "SHORT_BANK_CHARGES")]
    b = B([_it(0, 100000, party="C1")])  # same country -> PARTIAL
    assert run(_line(100000 - 2500), Hyp("t", 2, [0]), b) == [(0, 97500, "PARTIAL")]


def test_withholding_and_cn():
    b = B([_it(0, 100000, ent="BR01", party="C3"), _it(1, 20000, kind="ARC", ent="BR01", party="C3")])
    ctx = Ctx(_ms(), wht_rate={"C3": 150})
    assert run(_line(98500 - 20000), Hyp("t", 2, [0], cn=[1]), b, ctx) == [
        (0, 100000, "SHORT_WITHHOLDING"), (1, 20000, "")]


def test_n_to_m_single_partial():
    b = B([_it(0, 1000), _it(1, 2000), _it(2, 3000)])
    assert run(_line(1000 + 2000 + 1200), Hyp("t", 2, [0, 1, 2], partial_item=2), b) == [
        (0, 1000, ""), (1, 2000, ""), (2, 1200, "PARTIAL")]


def test_instalment_by_amount():
    # total 10001 in 3: piece 3333, last 3335; prior piece paid
    b = B([_it(0, 10001, open_=6668)], {0: [(0, 3333, "PARTIAL", D0)]})
    assert run(_line(3335), Hyp("t", 2, [0]), b) == [(0, 3335, "")]
    assert run(_line(3333), Hyp("t", 2, [0]), b) == [(0, 3333, "PARTIAL")]


def test_duplicate_and_return():
    b = B([_it(0, 5000, open_=0)], {0: [(0, 5000, "", D0)]})
    assert run(_line(5000), Hyp("t", 1, [0]), b) == [(0, 5000, "DUPLICATE_PAYMENT")]
    assert run(_line(-5000), Hyp("r", 1, [0], is_return=True), b) == [(0, 5000, "RETURNED")]


def test_advice_dispute_and_ret():
    b = B([_it(0, 10000), _it(1, 5000)])
    adv = {0: {"paid": 9000, "ded": 1000, "reason": "PRICE DIFF"}, 1: {"paid": 5000, "ded": 0, "reason": ""}}
    assert run(_line(14000), Hyp("advice", 0, [0, 1], advice=adv), b) == [(0, 9000, "SHORT_DISPUTE"), (1, 5000, "")]
    adv = {0: {"paid": 9000, "gross": 10000, "ded": 1000, "reason": "RET"}, 1: {"paid": 5000, "ded": 0, "reason": ""}}
    assert run(_line(14000), Hyp("advice", 0, [0, 1], advice=adv), b)[0] == (0, 10000, "SHORT_WITHHOLDING")


def test_no_fit_returns_none():
    b = B([_it(0, 1000), _it(1, 2000)])
    assert split_cash(_line(5000), Hyp("t", 2, [0, 1]), b, Ctx(_ms())) is None
