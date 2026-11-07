"""WP-B docs: advice parsers and PDF clause templates (literal phrasings from LIVE)."""

import zipfile
from pathlib import Path

import pytest

from recon2 import advices as A
from recon2 import pdfclauses as P
from recon2.model import DATA, Line, day


@pytest.mark.parametrize("s,v", [("1 076,45", 107645), ("1.299,28", 129928), ("2,328.37", 232837),
                                 ("25 366,13", 2536613), ("1.023.706,29", 102370629),
                                 ("7 629 426,81", 762942681), ("-231.15", -23115), ("-1,071.54", -107154)])
def test_money(s, v):
    assert A._money(s) == v and P._money(s) == v


def test_dec_no_float():
    assert A._dec("5079.2") == 507920 and A._dec("-18.38") == -1838 and A._dec("0") == 0


def test_txt_us_header_and_deduction(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("Subject: Payment advice REM/2025/0256\n\nPlease be advised that we have sent a payment of "
                 "2,500.27 USD on 06/25/2025 covering:\n\n"
                 "  1022514948         22/05/25          563.12  -11.26 DISCOUNT\n"
                 "  RE250103703        27/05/25        1,948.41\n\nKind regards,\n")
    a = A.Advice("a.txt", "US01", "x", 0, 0, "", "", 0, fmt="txt")
    A._txt(p, a)
    assert a.ref == "REM/2025/0256" and a.payment_date == day("2025-06-25") and a.ccy == "USD"
    assert [(r.doc, r.gross, r.ded, r.reason, r.paid) for r in a.rows][0] == ("1022514948", 56312, -1126, "DISCOUNT", 55186)
    assert a.ok and a.total == sum(r.paid for r in a.rows)


def _xlsx(path: Path, rows: list[list[str]]):
    cells = []
    for i, r in enumerate(rows, 1):
        cs = "".join(f'<c r="{chr(65 + j)}{i}" t="inlineStr"><is><t>{v}</t></is></c>' for j, v in enumerate(r) if v != "")
        cells.append(f'<row r="{i}">{cs}</row>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{''.join(cells)}</sheetData></worksheet>")


def test_xlsx_sep2025_voucher_layout(tmp_path):
    p = tmp_path / "v.xlsx"
    _xlsx(p, [["North Arden Landscapes Ltd."], ["Payment advice", "2638602637", "2026-01-02"],
              ["Voucher", "Invoice", "PO", "Inv. date", "Amount", "Deduction", "Net paid"],
              ["VCHR02593489", "430166", "4569664561", "2024-10-31", "1945.21", "-19.45", "1925.76"],
              ["VCHR12750294", "100769", "", "2025-11-17", "-5717.86", "0", "-5717.86"],
              ["TOTAL", "-3792.1"]])
    a = A.Advice("v.xlsx", "US01", "x", 0, 0, "", "", 0, fmt="xlsx")
    A._xlsx(p, a)
    assert a.ref == "2638602637" and a.payment_date == day("2026-01-02")
    r = a.rows[0]
    assert (r.doc, r.alias, r.po, r.gross, r.ded, r.paid) == ("430166", "VCHR02593489", "4569664561", 194521, -1945, 192576)
    assert a.rows[1].po == "" and a.total == -379210 == sum(x.paid for x in a.rows)


def test_index_match_ccy_and_ref():
    def adv(ref, ccy):
        return A.Advice(ref, "ES01", "p", 0, day("2025-06-30"), ref, ccy, 726121, [], True)
    ix = A.AdviceIndex([adv("PAY-2025-000001", "EUR"), adv("PAY-2025-001171", ""), adv("X", "USD")])
    ln = Line(0, "t", "acc", "ES01", "b", "n43", day("2025-06-30"), 0, 726121, "EUR", None, "", "", "", "",
              "", "TRANSF PAY-2025-001171 RODRIGUEZ", "", "", "", "")
    m = ix.match(ln)
    assert [a.file for a in m] == ["PAY-2025-001171", "PAY-2025-000001"]


def test_clauses_literal():
    E = P.extract
    f = E("TOTAL 13,000.10 AUD\nPayment terms: 2 instalments at 60/90 days (6,500.05 / 6,500.05\nAUD).")
    assert f["instalments"] == {"n": 2, "days": [60, 90], "amounts": [650005, 650005]} and f["total"] == 1300010
    f = E("Forma de pago: 3 vencimientos a 90/150/210 días (17.552,73 / 17.552,73 / 17.552,75 EUR).")
    assert f["instalments"]["amounts"] == [1755273, 1755273, 1755275]
    assert E("This invoice cancels and replaces invoice FT25-01-34545 dated 01/07/2025.")["replaces"] == "FT25-01-34545"
    assert E("Esta factura anula y sustituye a la factura 1012516816 de 30/06/2025, que")["replaces"] == "1012516816"
    assert E("Volume rebate Q2 2025 (1.0% on 28,725.21 USD): will be paid to your bank account.")["rappel"]["dir"] == "payout"
    assert E("Rappel T2 2025 por volumen (3.0 % s/ 35.802,66 EUR): se compensará en la próxima liquidación.")["rappel"]["dir"] == "offset"
    assert E("Este abono se compensará en sus próximos pagos.")["scn"] == "offset"
    assert E("The amount of this credit note will be refunded by bank transfer.")["scn"] == "refund"
    assert E("CANCELLING OF INVOICE Nº INV25-0115755")["cancel_cn"] == "INV25-0115755"
    r = E("Retention 5% (2,398.11 AUD) withheld until final acceptance (15/07/2026). Amount payable at due date: 45,564.18 AUD.")["retention"]
    assert r == {"pct": "5", "amount": 239811, "release": "2026-07-15", "payable": 4556418}
    assert E("A deducir anticipo recibido según proforma PF25-0100007 de 287,01 EUR. Importe neto a pagar 669,71 EUR.")["advance"] == \
        {"proforma": "PF25-0100007", "amount": 28701, "net": 66971}
    assert E("Cuota de mantenimiento y servicio técnico correspondiente a junio 2025.")["service_month"] == "2025-06"
    assert E("has been assigned to Confirming Peñalara. Payment is only discharging if made to it.")["assignment"] == "Confirming Peñalara"
    n = E("Please note that from 08/10/2025 our payments will be made from account CH1694696705567248844 "
          "(previously CH6094391291346652042).")["iban_change"]
    assert (n["new"], n["old"], n["effective"], n["dir"]) == ("CH1694696705567248844", "CH6094391291346652042", "2025-10-08", "payer")


@pytest.mark.skipif(not (DATA / "sample" / "remittance_advices").exists(), reason="no data")
def test_sample_advices_gate():
    advs = A.load_advices(DATA / "sample")
    assert len(advs) == 331 and all(a.ref and a.payment_date for a in advs)
    assert sum(a.ok for a in advs) >= 324   # 7 txt advices genuinely state less than their rows
    assert sum(len(a.rows) for a in advs if a.fmt == "csv") == 4966


@pytest.mark.skipif(not (DATA / "sample" / "pdfs").exists(), reason="no data")
def test_sample_pdf_invariants():
    facts = P.load_facts(DATA / "sample")
    ins = [f for f in facts.values() if "instalments" in f]
    assert len(ins) == 231 and all(abs(sum(f["instalments"]["amounts"]) - f["total"]) <= 1 for f in ins)
    assert sum("replaces" in f for f in facts.values()) == 200
