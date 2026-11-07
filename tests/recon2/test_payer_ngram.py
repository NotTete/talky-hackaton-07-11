from types import SimpleNamespace

from recon2.payer_ngram import PayerMatcher, norm


def _m():
    c = {"C1": {"name": "Prieto y Llorente Suministros S.L.", "trade_name": "", "entity": "ES00"},
         "C2": {"name": "Garrido Mantenimientos S.L.U.", "trade_name": "GARRIDO MANT", "entity": "ES00"},
         "C3": {"name": "Walker & Anderson Leisure Pty Ltd", "trade_name": "", "entity": "AU01"}}
    s = {"S1": {"name": "Garrido Mantenimientos S.A.", "entity": "ES00"}}
    return SimpleNamespace(customers=c, suppliers=s)


def test_norm_strips_legal():
    assert norm("Garrido Mantenimientos S.L.U.") == "GARRIDO MANTENIMIENTOS"


def test_match_truncated_deterministic():
    pm = PayerMatcher(_m()).build()
    r = pm.match("PRIETO Y LLORENTE SUMIN", "ES00", "AR")
    assert r[0][0] == "C1" and r == pm.match("PRIETO Y LLORENTE SUMIN", "ES00", "AR")
    assert pm.match("PRIETO Y LLORENTE", "AU01", "AR") == []
    assert pm.match("GARRIDO MANTENIMIENTOS", "ES00", "AP")[0][0] == "S1"
