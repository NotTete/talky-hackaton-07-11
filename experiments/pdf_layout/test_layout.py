"""Self-checks for the layout experiment (not collected by the repo's pytest config).

    python experiments/pdf_layout/test_layout.py

They pin the three things that were easy to get wrong, and each one cost real debugging
time: column counting, label/value pairing (table rows must not become pairs), and field
inference picking `total` over `base imponible`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "experiments"))

from pdf_layout.companies import _infer_fields, _priority  # noqa: E402
from pdf_layout.extract import _clusters, _lines, _pairs, Band, Chunk, read_pdf  # noqa: E402
from pdf_layout.tokens import classify  # noqa: E402

PDF_DIR = ROOT / "data" / "sample" / "pdfs"


def test_lines_and_columns_are_stable():
    chunks = [Chunk(x=10, y=100, size=8, text="A"), Chunk(x=200, y=100, size=8, text="1,00"),
              Chunk(x=10, y=90, size=8, text="B"), Chunk(x=200, y=90, size=8, text="2,00")]
    lines = _lines(chunks)
    assert [ln.text for ln in lines] == ["A 1,00", "B 2,00"]
    bands = _clusters([(c.x, c.x1) for c in chunks], gap=14)
    assert len(bands) == 2


def test_table_rows_are_not_label_value_pairs():
    # a row: description, qty, price, amount on separate baselines (as pypdf prints it)
    chunks = [Chunk(x=20, y=100, size=8, text="Widget"),
              Chunk(x=200, y=100, size=8, text="2"),
              Chunk(x=260, y=100, size=8, text="10,00"),
              Chunk(x=320, y=100, size=8, text="20,00")]
    lines = _lines(chunks)
    bands = _clusters([(c.x, c.x1) for c in chunks], gap=14)
    assert _pairs(lines, bands) == []


def test_token_typing_of_values():
    assert classify("1.752,08 EUR") == "AMOUNT"
    assert classify("30/06/2025") == "DATE"
    assert classify("ES8694022663690810607097") == "IBAN"
    assert classify("FA250222794") == "DOCNO"
    assert classify("Total TTC") != "AMOUNT"


def test_total_beats_base_imponible():
    pairs = [("Base imponible", "1.448,00 EUR"), ("IVA", "304,08 EUR"), ("TOTAL", "1.752,08 EUR")] * 5
    fields = _infer_fields(pairs, n_docs=5, vocab=None, name_set=None)
    assert fields["total"]["label"] == "total"
    assert _priority("Base imponible", "total") > _priority("total", "total")


def test_real_pdf_recovers_the_fields():
    files = sorted(PDF_DIR.glob("ES01/ARI_*.pdf")) or sorted(PDF_DIR.glob("*/ARI_*.pdf"))
    if not files:
        print("skip: no sample pdfs (run make data)")
        return
    doc = read_pdf(files[0])
    labels = {label.lower() for label, _ in doc.pairs}
    assert doc.n_bands >= 3
    assert {"nº", "fecha", "vencimiento"} & labels
    assert any(label.startswith("total") for label in labels)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} checks passed")
