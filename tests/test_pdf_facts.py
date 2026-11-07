"""PDF facts: the pieces that can silently be wrong.

No network, no downloads: the field/clause tests use strings, and the document test uses
one sample PDF when the data is there. These pin the four bugs that cost real time:
`Nº` folding to `no`, an empty "rest" making a table-row check always true, `1,752.08`
parsed as 1.75, and a clause accepted without its document/direction.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bankrec.pdf import clauses, fields  # noqa: E402
from bankrec.pdf.facts import DocFacts, PdfFacts  # noqa: E402


def test_money_accepts_both_decimal_conventions():
    assert fields.money("1.752,08") == 175208      # ES / FR / DE
    assert fields.money("1 752,08 EUR") == 175208  # space thousands
    assert fields.money("1,752.08") == 175208      # EN
    assert fields.money("1752.08") == 175208
    assert fields.money("1752,08") == 175208
    assert fields.money("12,5 kg") is None


def test_dates():
    assert fields.date("02/06/2025") == "2025-06-02"
    assert fields.date("2025-06-02") == "2025-06-02"
    assert fields.date("31.12.24") == "2024-12-31"
    assert fields.date("31/13/2025") is None


def test_docno_hint_survives_the_ordinal_indicator():
    # "Nº" folds to "no"; before the fix the label matched nothing and every doc lost its id
    pairs = [("Nº", "FV25-0110868"), ("Fecha", "02/06/2025"), ("TOTAL", "1.752,08 EUR")]
    resolved = fields.resolve(pairs, "FACTURA FV25-0110868 02/06/2025 EUR")
    assert resolved.document_number == "FV25-0110868"
    assert resolved.issue_date == "2025-06-02"
    assert resolved.total == 175208


def test_replaces_needs_a_document_and_direction_needs_a_word():
    text = "Esta factura anula y sustituye a la factura A-2024-55 de 01/02/2024. No abonar ambas."
    found = clauses.extract(text)
    assert clauses.replacement(found) == "A-2024-55"

    # "abono" alone is not actionable: the direction is the whole content
    assert not [c for c in clauses.extract("Se ha emitido un abono.") if c["code"] == "credit_note"]
    offset = clauses.extract("El importe de este abono se compensará en sus próximos pagos.")
    refund = clauses.extract("El importe de este abono les será reembolsado mediante transferencia.")
    assert offset and offset[0]["direction"] == "offset"
    assert refund and refund[0]["direction"] == "refund"

    instalments = clauses.extract("Payment terms: 3 instalments at 30/60/90 days")
    assert instalments and instalments[0]["count"] == 3

    month = clauses.extract("Cuota de mantenimiento y servicio técnico correspondiente a mayo 2025.")
    assert month and month[0]["month"] == "2025-05"


def test_facts_index_and_replacement_lookup():
    a = DocFacts(document_number="FV25-1", entity="ES01",
                 clauses=[{"code": "replaces", "document": "FV24-9", "negated": False}])
    b = DocFacts(document_number="FV24-9", entity="ES01")
    facts = PdfFacts([a, b])
    assert facts.for_docno("fv 25-1").document_number == "FV25-1"
    assert facts.replacement_for("FV24-9") == "FV251"


def test_real_document_round_trip():
    pdf = ROOT / "data/sample/pdfs/ES01/ARI_FV250110868.pdf"
    if not pdf.exists():
        pytest.skip("sample data missing")
    from bankrec.pdf.facts import from_pdf
    doc = from_pdf(pdf, entity="ES01", doc_type="invoice")
    assert doc.document_number == "FV25-0110868"
    assert doc.issue_date == "2025-06-02"
    assert doc.fields["total"] == 175208
    assert doc.checks["net_plus_tax"] is True
    assert not doc.flags
