"""FINAL-readiness: strict mode, FX fallback, masters_final, output coverage (data-free)."""

import gzip
from datetime import date
from fractions import Fraction

import pytest

from recon2 import engine, fx as fxm
from recon2.__main__ import verify_output


def _fx(tmp_path, rows):
    (tmp_path / "fx_rates.csv").write_text("date,currency,units_per_eur,source\n" + rows)
    return fxm.FX(tmp_path)


def test_fx_missing_currency_is_loud_not_fatal(tmp_path, capsys):
    f = _fx(tmp_path, "2025-12-30,USD,1.2,ECB\n")
    d = date(2026, 2, 1).toordinal()
    for _ in range(2):  # refuses to invent a rate (1 ZAR != 1 EUR): callers treat it as "no conversion"
        with pytest.raises(KeyError):
            f.rate("ZAR", d)
    assert capsys.readouterr().err.count("no FX rate for ZAR") == 1   # warned once
    assert f.missing["ZAR"] == 2
    assert f.rate("USD", d) == Fraction("1.2")                           # last known rate
    assert f.convert(100, "ZAR", "ZAR", d) == 100


def test_strict_phases(monkeypatch):
    monkeypatch.delenv("RECON2_STRICT", raising=False)
    assert engine.strict_for("live") and engine.strict_for("final") and engine.strict_for("final_sim")
    assert not engine.strict_for("sample") and not engine.strict_for("train:2025-05")
    monkeypatch.setenv("RECON2_STRICT", "1")
    assert engine.strict_for("sample")


class _Ph:
    class masters:
        class fx:
            missing = {}


def _eng(strict):
    e = engine.Engine.__new__(engine.Engine)
    e.strict, e.phase, e.errors = strict, _Ph, {"x:KeyError:boom": 5}
    return e


def test_error_threshold():
    with pytest.raises(RuntimeError):
        _eng(True).check_errors(1000)       # 0.5 % > 0.1 %
    _eng(True).check_errors(10000)          # 0.05 % tolerated
    _eng(False).check_errors(10)            # non-strict never aborts


class L:
    def __init__(self, t):
        self.txn = t


def _out(tmp_path, txns):
    p = tmp_path / "o.csv.gz"
    with gzip.open(p, "wt") as f:
        f.write("bank_txn_id,item_id,amount,category,exception\n")
        for t in txns:
            f.write(f"{t},,,UNIDENTIFIED,UNIDENTIFIED\n")
    return p


def test_verify_output_coverage(tmp_path):
    verify_output([L("A"), L("B")], _out(tmp_path, ["A", "B"]))
    with pytest.raises(RuntimeError):
        verify_output([L("A"), L("B")], _out(tmp_path, ["A"]))
    with pytest.raises(RuntimeError):
        verify_output([L("A")], _out(tmp_path, ["A", "Z"]))


def test_phase_registry_and_masters_final():
    import recon2.load as ld
    assert "final_sim" in ld.PHASES
    assert 'root / "masters_final"' in open(ld.__file__).read()
