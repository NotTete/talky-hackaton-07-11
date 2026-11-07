"""Payer identification: the failure mode that is invisible in the score.

A bank statement prints our customer's own name, cut at the column width, and the masters
are full of siblings that share a word with it. Matching that by *tokens* picked the
wrong company on 273 of the sample's 752 missed lines, because `name_similarity` divides
by the *shorter* name: "Spa Alonso SARL" has one distinctive token, so it covers
"ALONSO HIDROTECNIA S.A" perfectly and outscores the customer that is actually named.
Matching it by *prefix* names the customer outright. The names below are the real ones.
"""

from __future__ import annotations

import polars as pl

from bankrec.alt.engine import _name_readings
from bankrec.alt.ledger import Ledger
from bankrec.text import name_similarity, name_tokens

NAMES = {
    "CES01000737": "Spa Sol S.A.",
    "CBR01000081": "Rega Sol S.A.",
    "CBR01000533": "Águas Sol S.A.",
    "CES01003074": "Alonso Hidrotecnia S.A.",
    "CFR01001722": "Spa Alonso SARL",
    "CES01001783": "Villamar del Río Riegos S.L.",
    "CES01000014": "Villamar del Río Camping S.L.",
    "CES01001739": "Sierramayor Construcciones S.L.",
    "CES01000476": "Instalaciones Sierramayor S.A.",
    "CES01000318": "Climatización Puerto Alcor S.L.",
}


class _Masters:
    party_names = NAMES
    iban_owner: dict = {}
    terms: dict = {}
    own_accounts: dict = {}


def _ledger() -> Ledger:
    empty = pl.DataFrame(
        schema={
            "item_id": pl.Utf8, "party_id": pl.Utf8, "document_number": pl.Utf8,
            "po_reference": pl.Utf8, "open_cents": pl.Int64, "total_cents": pl.Int64,
            "issue_date": pl.Utf8, "due_date": pl.Utf8,
        }
    )
    return Ledger(empty, _Masters())


def test_token_voting_alone_scores_the_wrong_company_higher():
    """Why the prefix index exists, stated as the arithmetic that went wrong."""
    bank = name_tokens("ALONSO HIDROTECNIA S.A")
    assert name_similarity(bank, name_tokens(NAMES["CFR01001722"])) == 1.0   # Spa Alonso
    assert name_similarity(bank, name_tokens(NAMES["CES01003074"])) < 1.0    # the real one


def test_exact_name_identifies_the_party():
    assert _ledger().parties_by_name_prefix("SPA SOL S.A.") == ["CES01000737"]
    assert _ledger().parties_by_name_prefix("ALONSO HIDROTECNIA S.A") == ["CES01003074"]


def test_truncated_name_identifies_the_party():
    # "TRANSFERENCIAS VILLAMAR DEL RIO RIEGO |" -- the bank cut the trailing S.
    assert _ledger().parties_by_name_prefix("VILLAMAR DEL RIO RIEGO") == ["CES01001783"]


def test_siblings_are_told_apart_by_their_prefix():
    assert _ledger().parties_by_name_prefix("SIERRAMAYOR CONSTRUCCI") == ["CES01001739"]
    assert _ledger().parties_by_name_prefix("INSTALACIONES SIERRAMA") == ["CES01000476"]


def test_a_word_is_not_an_identification():
    assert _ledger().parties_by_name_prefix("SOL") == []
    assert _ledger().parties_by_name_prefix("SPA") == []


def test_an_ambiguous_prefix_returns_the_shortlist_not_a_guess():
    # Two siblings start the same way; the amount and the reference decide, not the name.
    assert sorted(_ledger().parties_by_name_prefix("VILLAMAR DEL RIO")) == [
        "CES01000014",
        "CES01001783",
    ]


def test_a_proforma_is_not_an_invoice():
    """POLICIES §4: paying a proforma settles nothing, so no resolver may allocate."""
    from bankrec.alt.cats import Categories
    from bankrec.alt.engine import ADVANCE_WORDS

    assert "PROFORMA" in ADVANCE_WORDS
    cats = Categories(_Masters())
    advance = {"amount_cents": 500000, "text": "CONCEPTO: ANTICIPO PROFORMA PF25-0400006"}
    assert cats.of(advance) == "CUSTOMER_ADVANCE"
    # ... but a deposit whose whole concept is the bare word is an unexplained receipt.
    bare = {"amount_cents": 500000, "text": "INGRESO CAJERO SUC 0504 | ANTICIPO"}
    assert cats.of(bare) == "UNIDENTIFIED"


def test_a_name_glued_behind_a_tax_id_is_still_a_name():
    """"TRANSFERENCIA SEPA | 27945126-CLIMATIZACION PUERTO ALCOR S. | L | PAGO JUNIO".

    No marker word, a tax id hyphenated in front, the legal form spilling into the next
    field. 199 of the sample's missed lines looked like this.
    """
    text = "TRANSFERENCIA SEPA | 27945126-CLIMATIZACION PUERTO ALCOR S. | L | PAGO JUNIO"
    found = {p for r in _name_readings(text) for p in _ledger().parties_by_name_prefix(r)}
    assert found == {"CES01000318"}


def test_free_text_fields_name_nobody():
    """The readings are generous because the lookup is strict: junk matches nothing."""
    for reading in _name_readings("TRANSFER INMEDIATA | PAGO JUNIO | LIQUIDACION"):
        assert _ledger().parties_by_name_prefix(reading) == []
