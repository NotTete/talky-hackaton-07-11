"""Integration-layer tests: writer guards/format, resolver helpers, gap classifier.
Fast and data-free; the end-to-end check is `python -m recon2 run --phase sample`."""

import gzip
from datetime import date

from recon2 import gap, resolve as R, writer
from recon2.model import Alloc, Decision, Item, Line


def _item(idx, iid, kind="ARI", total=10000, open_=None, party="C1", doc="FV25-0000001", ccy="EUR", issue=None):
    d = issue or date(2025, 5, 1).toordinal()
    return Item(idx, iid, kind, "ES01", party, "", doc, d, d + 30, ccy, total, total if open_ is None else open_)


def _line(cash, text="", ccy="EUR", d=None):
    return Line(0, "BT-2025-0000001", "ES01-X", "ES01", "X", "n43", d or date(2025, 6, 2).toordinal(), 0,
                cash, ccy, None, "", "", "", "", "", text, "", "", "", "", t_join=text)


def test_fmt_cents():
    assert writer.fmt_cents(123456) == "1234.56"
    assert writer.fmt_cents(5) == "0.05"
    assert writer.fmt_cents(100) == "1.00"


def test_guard_rejects_bad_rows():
    ids = ["ARI-2025-0000001", "ARI-2025-0000002", "BAD"]
    assert writer.guard([Alloc(0, 100, "", "E5")], ids) is None
    assert writer.guard([Alloc(0, 0, "", "E5")], ids)
    assert writer.guard([Alloc(2, 100, "", "E5")], ids)
    assert writer.guard([Alloc(0, 100, "", ""), Alloc(0, 100, "", "")], ids)
    assert writer.guard([Alloc(0, 100, "OVERPAYMENT", ""), Alloc(1, 100, "", "")], ids)
    assert writer.guard([Alloc(0, 100, "SHORT_FEE", "")], ids)


def test_write_demotes_and_covers_every_line(tmp_path):
    ids = ["ARI-2025-0000001", "ARI-2025-0000002"]
    decs = [Decision("BT-2025-0000001", [Alloc(0, 100, "", "E5"), Alloc(0, 100, "", "E5")]),
            Decision("BT-2025-0000002", category="UNIDENTIFIED", exc="UNIDENTIFIED"),
            Decision("BT-2025-0000003", [Alloc(1, 250, "PARTIAL", "E10")])]
    p = tmp_path / "s.csv.gz"
    st = writer.write(decs, ids, p, {"BT-2025-0000001": "BANK_FEES"})
    rows = gzip.decompress(p.read_bytes()).decode().splitlines()
    assert rows[0] == "bank_txn_id,item_id,amount,category,exception"
    assert rows[1] == "BT-2025-0000001,,,BANK_FEES,"
    assert rows[2] == "BT-2025-0000002,,,UNIDENTIFIED,UNIDENTIFIED"
    assert rows[3] == "BT-2025-0000003,ARI-2025-0000002,2.50,,PARTIAL"
    assert st["demoted"] == 1


def test_write_is_deterministic(tmp_path):
    decs = [Decision("BT-2025-0000002", category="BANK_FEES")]
    a, b = tmp_path / "a.csv.gz", tmp_path / "b.csv.gz"
    writer.write(decs, [], a, {})
    writer.write(decs, [], b, {})
    assert a.read_bytes() == b.read_bytes()


def test_subset_exact_bounded():
    assert sorted(R._subset_exact([500, 300, 200, 700], 1000, 8)) in ([0, 1, 2], [1, 3])
    assert R._subset_exact([500, 300], 1, 8) is None


def test_month_regex():
    m = R.MONTH_RE.search("CUOTA MANTENIMIENTO MAYO 2025")
    assert m and R.MONTHS[m.group(1)] == 5 and m.group(2) == "2025"


class _Book:
    def __init__(self, items):
        self.items = items
        self.hist = {}

    def by_doc(self, t):
        return [it.idx for it in self.items if it.doc.replace("-", "") == t]

    def by_doc_suffix(self, t, scope=None):
        return []

    def by_doc_prefix(self, t, scope=None):
        return []

    def open_of_party(self, p, kinds):
        return sorted((it.idx for it in self.items if it.party_id == p and it.kind in kinds and it.open > 0),
                      key=lambda i: (self.items[i].due, i))


def test_party_amount_needs_party_and_prefers_unique():
    items = [_item(0, "ARI-2025-0000001", total=10000), _item(1, "ARI-2025-0000002", total=22345),
             _item(2, "ARI-2025-0000003", total=10000, party="C2")]
    res = R.Resolver(_Book(items))
    ln = _line(10000)
    assert res.by_party_amount(ln, [], {}) == []           # never amount alone
    h = res.by_party_amount(ln, ["C1"], {})
    assert h and h[0].items == [0] and h[0].tier == 4
    h = res.by_party_amount(_line(32345), ["C1"], {})     # oldest prefix / subset
    assert h and sorted(h[0].items) == [0, 1]


def test_ref_named_partial_and_dup():
    items = [_item(0, "ARI-2025-0000001", total=10000, doc="FV250000001")]
    res = R.Resolver(_Book(items))
    ln = _line(4000, "FRA FV250000001")
    ln.tokens = ("FV250000001",)
    h = res.by_ref(ln, set(), {})
    assert h and h[0].items == [0]


def test_gap_classify():
    g = {"items": {"ARI-2025-0000001": (10.0, "")}, "cat": "", "exc": "", "seg": "x"}
    s = {"items": {}, "cat": "UNIDENTIFIED", "exc": "UNIDENTIFIED"}
    b, fn, fp = gap.classify(g, s, {"reason": "NO_PARTY", "cands": []})
    assert b == "NOT_GENERATED(NO_PARTY)" and fn == 1 and fp == 0
    s = {"items": {"ARI-2025-0000001": (10.0, "PARTIAL")}, "cat": "", "exc": ""}
    b, *_ = gap.classify(g, s, {"rows": [["ARI-2025-0000001", 1000, "PARTIAL", "E10"]]})
    assert b.startswith("EXC_WRONG(E10")
