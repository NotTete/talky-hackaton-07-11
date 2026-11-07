"""Ops / returns: amount gate, direction, fee-line refusal, e2e, returns by mandate/cheque."""

from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace

from recon2.model import Item, Line, day
from recon2.ops import Ops, _subset
from recon2.returns import Returns

DD = ("batch_id,scheme,collection_date,account_id,currency,batch_total,end_to_end_id,mandate_id,"
      "debtor_name,debtor_iban,amount,remittance_info,re_presentation\n"
      "03250603661,SEPA CORE,2025-06-03,ES01-PENA-EUR,EUR,300.00,03250603661-0001,VTM-ES-0000001,A SL,ES1,100.00,FV25-0000001,N\n"
      "03250603661,SEPA CORE,2025-06-03,ES01-PENA-EUR,EUR,300.00,03250603661-0002,VTM-ES-0000002,B SL,ES2,200.00,RECIBO 0000002,N\n")
RUN = ("run_id,run_type,execution_date,debtor_account_id,end_to_end_id,beneficiary,beneficiary_iban,currency,"
       "transfer_amount,document_number,document_type,document_amount,remittance_info\n"
       "PR010025061275,transfers,2025-06-12,DE01-RHEI-EUR,PR010025061275-0001,Sup A,DE1,EUR,90.00,INV1001,INVOICE,100.00,x\n"
       "PR010025061275,transfers,2025-06-12,DE01-RHEI-EUR,PR010025061275-0001,Sup A,DE1,EUR,90.00,RINV1001,CREDIT_NOTE,-10.00,x\n"
       "PR010025061275,transfers,2025-06-12,DE01-RHEI-EUR,PR010025061275-0002,Sup B,DE2,EUR,50.00,INV2002,INVOICE,50.00,x\n")
CHQ = ("deposit_id,deposit_date,account_id,currency,deposit_total,cheque_number,drawer,drawer_bank_routing,amount,stub_reference\n"
       "RCH25060202275,2025-06-02,FR01-LORV-EUR,EUR,40.00,0741845,C SARL,1,40.00,FA250000003\n")


class FakeBook:
    def __init__(self, items):
        self.items = items
        self.by_id = {it.item_id: it.idx for it in items}
        self.hist = {}
        self._doc = defaultdict(list)
        for it in items:
            self._doc[it.doc].append(it.idx)

    def by_doc(self, t):
        return list(self._doc.get(t, []))

    def by_doc_suffix(self, t, scope=None):
        t = "".join(c for c in t if c.isalnum())
        return [it.idx for it in self.items if it.doc.replace("-", "").endswith(t)
                and (scope is None or it.entity in scope)]

    def open_of_party(self, p, kinds):
        return [it.idx for it in self.items if it.party_id == p and it.kind in kinds and it.open > 0]


def _item(i, iid, ent, doc, total, party="P"):
    return Item(i, iid, iid[:3], ent, party, "", doc, 0, 0, "EUR", total, total)


def _line(cash, text, ent="ES01", acct="ES01-PENA-EUR", e2e=""):
    return Line(0, "BT1", acct, ent, "X", "n43", day("2025-06-20"), 0, cash, "EUR", None, "",
                "", "", "", "", text, "", "", e2e, "")


def _setup(tmp_path):
    for sub, name, body in (("direct_debit_batches", "ES01_MAD_202506.csv", DD),
                            ("payment_runs", "DE01_202506_0.csv", RUN),
                            ("cheque_deposits", "FR01_LYO_202506.csv", CHQ)):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / name).write_text(body)
    items = [_item(0, "ARI-2025-0000001", "ES01", "FV25-0000001", 10000),
             _item(1, "ARI-2025-0000002", "ES01", "FV25-0000002", 20000),
             _item(2, "API-2025-0000003", "DE01", "INV1001", 10000),
             _item(3, "APC-2025-0000004", "DE01", "RINV1001", 1000),
             _item(4, "API-2025-0000005", "DE01", "INV2002", 5000),
             _item(5, "ARI-2025-0000006", "FR01", "FA250000003", 4000)]
    phase = SimpleNamespace(root=tmp_path, masters=SimpleNamespace(customers={}))
    ops = Ops(phase, [])
    return ops, Returns(phase, ops), FakeBook(items)


def test_dd_batch_and_fee_line(tmp_path):
    ops, _, book = _setup(tmp_path)
    h = ops.resolve(_line(30000, "ABONO REMESA SEPA CORE 03250603661 2 RECIBOS"), book)
    assert h and h.tier == 0 and sorted(h.items) == [0, 1]       # suffix lookup for RECIBO 0000002
    assert ops.resolve(_line(-1875, "COMISION REMESA 03250603661 2 RECIBOS"), book) is None
    assert ops.resolve(_line(-30000, "REMESA 03250603661"), book) is None   # wrong direction


def test_run_e2e_with_credit_note(tmp_path):
    ops, _, book = _setup(tmp_path)
    h = ops.resolve(_line(-9000, "SEPA-UEBERWEISUNG PR010025061275-0001", "DE01", "DE01-RHEI-EUR"), book)
    assert h and h.items == [2] and h.cn == [3]
    whole = ops.resolve(_line(-14000, "SAMMEL PR010025061275", "DE01", "DE01-RHEI-EUR"), book)
    assert whole and sorted(whole.items) == [2, 4]
    assert ops.resolve(_line(9000, "PR010025061275-0001", "DE01", "DE01-RHEI-EUR"), book) is None


def test_returns(tmp_path):
    ops, rets, book = _setup(tmp_path)
    h = rets.resolve(_line(-20000, "DEVOLUCION RECIBO MD06 REM 03250603661 MANDATO VTM-ES-0000002"), book)
    assert h and h.is_return and h.tier == 1 and h.items == [1]
    assert rets.resolve(_line(-600, "COMISION DEVOLUCION RECIBO VTM-ES-0000002"), book) is None
    c = rets.resolve(_line(-4000, "CHEQUE IMPAYE 0741845 NSF", "FR01", "FR01-LORV-EUR"), book)
    assert c and c.items == [5]
    ap = rets.resolve(_line(5000, "DEVOLUCION TRANSFERENCIA AC04 PR010025061275-0002", "DE01", "DE01-RHEI-EUR"), book)
    assert ap and ap.items == [4] and ap.is_return
    assert rets.resolve(_line(-20000, "ANULACION OPERACION INDEBIDA VTM-ES-0000002"), book) is None


def test_subset_unique_and_count():
    assert _subset([5, 7, 11], 18, 0) == [1, 2]
    assert _subset([5, 5, 10], 10, 2) is None or _subset([5, 5, 10], 10, 2) == [0, 1]
    assert _subset([1, 2, 3], 100, 0) is None
