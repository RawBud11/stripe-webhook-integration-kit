"""Our verifier against the committed vectors. Needs no optional dependency."""

import make_vectors
import pytest
from vectors import CASES, VECTORS_PATH, our_outcome


def test_committed_vectors_match_generator():
    """Fails if the vectors file and tools/make_vectors.py drift apart."""
    assert VECTORS_PATH.read_bytes() == make_vectors.render(), (
        "signature_vectors.json is out of date; run: python tools/make_vectors.py"
    )


def test_case_names_are_unique():
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names))


def test_every_difference_is_explained_and_only_differences_are():
    for case in CASES:
        agrees = (case["ours"] == "valid") == (case["library"] == "accepted") and case[
            "library"
        ] in ("accepted", "SignatureVerificationError")
        assert (case["difference"] is None) == agrees, case["name"]


def test_small_future_skew_is_accepted_by_us():
    (case,) = [c for c in CASES if c["name"] == "future_small_clock_skew"]
    assert 0 < int(case["header"].split(",")[0][2:]) - case["now"] <= case["tolerance"]
    assert our_outcome(case) == "valid"


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_our_verifier(case):
    assert our_outcome(case) == case["ours"]
