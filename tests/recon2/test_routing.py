"""WP-C routing: treasury regex, category router overrides, party resolution precedence."""
import json
from types import SimpleNamespace

from recon2 import cats, party, treasury
from recon2.model import Line


def L(text="", cash=-100, fmt="n43", code="04", sub="", cpty="", acct="", rinfo="", entity="ES01"):
    return Line(idx=0, txn="T", account="A", entity=entity, bank="PENA", fmt=fmt, day=1, value_day=1, cash=cash,
                ccy="EUR", orig=None, orig_ccy="", code=code, subcode=sub, ref1="", ref2="", text=text,
                cpty_name=cpty, cpty_acct=acct, e2e="", rinfo=rinfo)


def test_treasury():
    assert treasury.classify(L("TRASPASO CASH POOLING NIVELACION SALDO")) == "CASH_POOL"
    assert treasury.classify(L("CASH CONCENTRATION ES02 9690")) == "CASH_POOL"
    assert treasury.classify(L("TRASPASO ENTRE CUENTAS ES00")) == "CASH_POOL"
    assert treasury.classify(L("TRASPASO A CTA 1447")) == "INTERNAL_TRANSFER"
    assert treasury.classify(L("FX DEAL 2109039996 EUR/USD")) == "FX_DEAL"
    assert treasury.classify(L("COMPRA DIVISA USD 1.000.000 OP 2402073 | 142")) == "FX_DEAL"
    assert treasury.classify(L("TRANSF. IC VALTERMIA NORTH AMERICA INC | .")) == "INTERCOMPANY"
    assert treasury.classify(L("CHAPS  FINANCIACION IC VA")) == "INTERCOMPANY"
    # traps: bare POOL / PRESTAMO / TRASPASO are not treasury
    assert treasury.classify(L("ACH CREDIT MARTIN POOL SUPP CCD")) is None
    assert treasury.classify(L("CARGO POR AMORTIZACION DE PRESTAMO DE01-6279771428")) is None
    assert treasury.classify(L("TRASPASO")) is None


def test_router(tmp_path):
    art = tmp_path / "r.json"
    lv = [{}, {}, {}, {"D": {"BANK_FEES": 3}, "C": {"UNIDENTIFIED": 3}}]
    lv[2]["D|NOMINA 99"] = {"PAYROLL": 9}
    art.write_text(json.dumps({"levels": lv}))
    r = cats.Router(art)
    assert r.category(L("NOMINA 09 21 -", cash=-5)) == "PAYROLL"
    assert r.category(L("PROFORMA PF25-0100001", cash=5)) == "CUSTOMER_ADVANCE"
    assert r.category(L("DEVOLUCION PAGO DUPLICADO FV24-0323", cash=-5)) == "CUSTOMER_REFUND"
    assert r.category(L("REEMBOLSO FAC2100102", cash=5)) == "SUPPLIER_REFUND"
    assert r.category(L("LIQUIDACION INTERESES 1T24", cash=-5)) == "INTEREST_EXPENSE"
    assert r.category(L("LIQUIDACION INTERESES 1T24", cash=5)) == "INTEREST_INCOME"
    assert r.category(L("FX DEAL 2109039996 EUR/USD", cash=5)) == "FX_DEAL"
    assert r.category(L("XYZ", cash=5)) == "UNIDENTIFIED"
    assert r.category(L("XYZ", cash=-5)) == "BANK_FEES"


def _masters():
    c = {
        "CES01000001": dict(name="Mantenimientos Blue S.L.", trade_name="", iban="ES1111", parent="", entity="ES01"),
        "CES01000002": dict(name="Riegos Crespo Hermanos S.L.U.", trade_name="", iban="", parent="CES01000003", entity="ES01"),
        "CES01000003": dict(name="Grupo Crespo Holding S.A.", trade_name="", iban="ES2222", parent="", entity="ES01"),
        "CES01000004": dict(name="Agua Viva S.A.", trade_name="", iban="ES44000ZZZ1", parent="", entity="ES01"),
    }
    s = {"SES01000001": dict(name="OV8 Valves S.n.c.", iban="", entity="ES01")}
    return SimpleNamespace(customers=c, suppliers=s)


def test_party_precedence():
    p = party.Parties(_masters(), None)
    # confirming: ORD. <debtor> replaces the factor; doc glued across the 38-char wrap
    ln = L("FATIR277457612 | ORD. MANTENIMIENTOS BLUE S.L. FRA FV24-05 | 07015 VTO 160624", cash=100)
    assert [x[0] for x in p.resolve(ln, "AR")] == ["CES01000001"]
    # agency EXPTE: debtor, never the agency
    ln = L("TRANSFER INMEDIATA | 67026408-RECOBROS AGORA, S.L. | EXPTE 62378 RIEGOS CRESPO HERMANOS 0320382", cash=100)
    assert p.resolve(ln, "AR")[0] == ("CES01000002", "third_party")
    assert "0320382" in p.third_party_doc_hint(ln)
    # agency without EXPTE: no party
    assert p.resolve(L("ACH CREDIT ARVENTO RECOVERIES LTD", cash=100), "AR") == []
    # IBAN of the parent expands to its children; creditor ids are ignored
    r = p.resolve(L("TRANSFER", cash=100, acct="ES22 22"), "AR")
    assert r[0] == ("CES01000003", "iban") and ("CES01000002", "child") in r
    assert p.resolve(L("TRANSFER", cash=100, acct="ES44000ZZZ1"), "AR") == []
    # truncated name prefix, names with digits, AP side
    assert p.resolve(L("TRANSFERENCIA DE RIEGOS CRESPO HERM", cash=100), "AR")[0][0] == "CES01000002"
    assert p.resolve(L("BONIFICO A FAVORE DI OV8 VALVES S.N.C. CAUSALE FRA A002627"), "AP")[0][0] == "SES01000001"
