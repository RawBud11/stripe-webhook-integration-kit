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


# Our rule: |now - t| <= tolerance is accepted, so both edges are inclusive.
BOUNDARIES = {
    "past_at_tolerance_edge": (-300, "valid"),
    "expired": (-301, "TimestampOutsideToleranceError"),
    "future_small_clock_skew": (5, "valid"),
    "future_at_tolerance_edge": (300, "valid"),
    "future_beyond_tolerance": (301, "TimestampOutsideToleranceError"),
}


@pytest.mark.parametrize("name", BOUNDARIES)
def test_timestamp_boundaries(name):
    (case,) = [c for c in CASES if c["name"] == name]
    offset, expected = BOUNDARIES[name]
    assert case["tolerance"] == 300
    assert int(case["header"].split(",")[0][2:]) - case["now"] == offset
    assert our_outcome(case) == expected


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_our_verifier(case):
    assert our_outcome(case) == case["ours"]
