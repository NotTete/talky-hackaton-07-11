"""The miss-report's cause classifier: pure logic, no data needed."""

from bankrec.misses import cause_of


def test_no_probe_means_no_trace():
    assert cause_of(None) == "no_trace"


def test_no_reference_in_text():
    assert cause_of({"tok": [], "steps": []}) == "no_reference"


def test_open_beats_absent_across_references():
    probe = {"tok": [["RHEI2506023297378", "absent"], ["FV250307", "open", 3, 3]]}
    assert cause_of(probe) == "ref_open"


def test_settled_partial_absent_ranking():
    assert cause_of({"tok": [["X1", "absent"], ["X2", "settled", 1, 0]]}) == "ref_settled"
    assert cause_of({"tok": [["X1", "absent"], ["X2", "partial"]]}) == "ref_partial"
    assert cause_of({"tok": [["X1", "absent"]]}) == "ref_absent"
