"""Cross-check against Stripe's official Python library, offline.

Skipped unless the optional `test` extra (stripe) is installed. The library is used only
to verify; it never generates signatures here. `WebhookSignature.verify_header` makes no
network call and needs no API key (read in stripe 16.0.0 `stripe/_webhook.py`).
"""

import random
import time

import pytest
import simulate
from vectors import CASES, SECRET, body_of

stripe = pytest.importorskip("stripe")

AGREEING = [c for c in CASES if c["difference"] is None]
DIFFERENCES = [c for c in CASES if c["difference"] is not None]


def library_outcome(body: bytes, header: str, secret: str, now: int, tolerance: int) -> str:
    """Run the library's verifier with the clock fixed at `now`."""
    real_time = time.time
    time.time = lambda: now
    try:
        stripe.WebhookSignature.verify_header(body, header, secret, tolerance=tolerance)
    except Exception as exc:  # we record which exception, whatever it is
        return type(exc).__name__
    finally:
        time.time = real_time
    return "accepted"


def library_outcome_for(case: dict) -> str:
    return library_outcome(body_of(case), case["header"], SECRET, case["now"], case["tolerance"])


def test_library_version_is_the_one_documented():
    assert stripe.VERSION == "16.0.0"


@pytest.mark.parametrize("case", AGREEING, ids=lambda c: c["name"])
def test_library_agrees_with_vector(case):
    assert library_outcome_for(case) == case["library"]


@pytest.mark.library_behaviour
@pytest.mark.parametrize("case", DIFFERENCES, ids=lambda c: c["name"])
def test_documents_library_behaviour(case):
    """Documents stripe's observed behaviour where it differs from ours.

    If this fails after a stripe upgrade, the library changed: update the vector's
    'library' value and the README table, after checking the new behaviour.
    """
    assert library_outcome_for(case) == case["library"], case["difference"]


# --- simulator output, checked by the library ----------------------------------------

SIM_SECRET = "whsec_test_fake_crosscheck_simulator"
NOW = 1_790_000_000


def simulated(variant=None, **kwargs):
    return simulate.build_requests(
        secret=SIM_SECRET, now=NOW, rng=random.Random(11), variant=variant, **kwargs
    )


def check(request) -> str:
    header = request.headers.get("Stripe-Signature", "")
    return library_outcome(request.body, header, SIM_SECRET, NOW, 300)


@pytest.mark.parametrize(
    "variant, kwargs",
    [(None, {}), (None, {"with_v0": True}), ("rolled_secret", {}), ("duplicate", {})],
)
def test_library_accepts_simulator_output(variant, kwargs):
    for request in simulated(variant, **kwargs):
        assert check(request) == "accepted", request.label


@pytest.mark.parametrize(
    "variant",
    ["tamper_body", "wrong_secret", "expired", "missing_header", "malformed_header"],
)
def test_library_rejects_broken_simulator_output(variant):
    (request,) = simulated(variant)
    assert check(request) == "SignatureVerificationError"
