from fractions import Fraction

import pytest

from recon2.book import Book
from recon2.load import load_phase
from recon2.model import Alloc, Item, Line, day
from recon2.money import cents, money
from recon2.textviews import build, fold, tokens_of


@pytest.mark.parametrize("s,v", [
    ("1 076,45", 107645), ("1.299,28", 129928), ("2,328.37", 232837), ("25 366,13", 2536613),
    ("1.023.706,29", 102370629), ("7 629 426,81", 762942681), ("-231.15", -23115), ("1,752.08", 175208),
    ("1 076,45", 107645), ("7 629 426,81", 762942681), ("12", 1200), ("1.023.706", 102370600),
])
def test_money(s, v):
    assert money(s) == v


def test_money_ambiguous():
    with pytest.raises(ValueError):
        money("1.023")


def test_cents():
    assert cents("-1487.60") == -148760
    assert cents("") == 0 and cents(None) == 0
    assert cents("3.5") == 350 and cents("12") == 1200 and cents("0.005") == 1


def _line(text="", ref1="", ref2="", rinfo="", e2e="", cpty=""):
    return Line(0, "BT", "A", "ES01", "B", "n43", 1, 1, 100, "EUR", None, "", "", "", ref1, ref2, text, cpty, "",
                e2e, rinfo)


def test_fold():
    assert fold("Weißenau  Café-Ü/1") == "WEIENAU CAFE-U/1"


def test_wrap_glue():
    frag = "TRANSFERENCIA A DOMINGUEZ Y REDONDO JA"
    assert len(frag) == 38
    ln = _line(text=frag + " | RDINES S")
    build(ln)
    assert "REDONDO JARDINES" in ln.t_join
    ln = _line(text="SHORT | NEXT")
    build(ln)
    assert "SHORT NEXT" in ln.t_join


def test_tokens():
    t = tokens_of("SEPA EREF+NOTPROVIDED SVWZ+RECHNUNGRE250110905")
    assert "RE250110905" in t and "RECHNUNGRE250110905" in t
    t = tokens_of("SVWZ+RE RE250106232 106234 106402")
    assert {"RE250106232", "RE250106234", "RE250106402"} <= set(t)
    assert list(t) == sorted(set(t))
    assert "FV250414882" in tokens_of("FRAS FV25-0414882")


def _item(i, iid, doc, party, total, due=10, po=""):
    return Item(i, iid, iid[:3], "ES01", party, "", doc, 1, due, "EUR", total, total, po=po)


def test_book():
    items = [_item(0, "ARI-1", "FV25-0414882", "C1", 1000), _item(1, "ARI-2", "FV25-0414883", "C1", 500, due=5),
             _item(2, "API-1", "FAC001", "S1", 700), _item(3, "API-2", "FAC001", "S2", 700, po="PO123456")]
    b = Book(items, None)
    assert b.by_doc("FV25-0414882") == [0]
    assert b.by_doc("250414882") == [0]
    assert b.by_doc("FAC001") == [2, 3]
    assert b.by_doc("S2|FAC001") == [3]
    assert b.by_doc("PO123456") == [3]
    assert b.by_doc_suffix("0414882") == [0]
    assert b.by_doc_prefix("FV250414") == [0, 1]
    assert b.by_doc_prefix("FV250414", scope={"C2"}) == []
    assert b.open_of_party("C1", ("ARI",)) == [1, 0]
    assert b.open_by_amount("ES01", "EUR", 700) == [2, 3]
    ln = _line()
    b.apply(ln, [Alloc(0, 400, "PARTIAL", "E10")])
    assert items[0].open == 600 and items[0].residue and b.open_by_amount("ES01", "EUR", 600) == [0]
    b.apply(ln, [Alloc(0, 600, "", "E5")])
    assert b.settled(0) and b.open_of_party("C1", ("ARI",)) == [1]
    b.apply(ln, [Alloc(0, 1000, "RETURNED", "E1")])
    assert items[0].open == 1000 and b.open_of_party("C1", ("ARI",)) == [1, 0]
    assert b.open_by_amount("ES01", "EUR", 1000) == [0]


def test_load_sample():
    ph = load_phase("sample")
    assert ph.lines and ph.items and ph.golden is not None
    assert all(a.idx == i for i, a in enumerate(ph.lines))
    keys = [(ln.day, ln.txn) for ln in ph.lines]
    assert keys == sorted(keys)
    assert all(it.kind == it.item_id[:3] for it in ph.items)
    assert ph.masters.terms["D2N30"] == (30, 200, 10)
    assert ph.masters.fx.rate("EUR", day("2025-06-02")) == Fraction(1)
    assert ph.masters.fx.rate("USD", day("2025-06-02")) > 0
    assert ph.masters.fx.convert(10000, "EUR", "EUR", 1) == 10000
