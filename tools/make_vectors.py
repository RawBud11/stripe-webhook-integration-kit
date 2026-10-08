"""Generate tests/fixtures/signature_vectors.json.

Each case records what our verifier must do and what Stripe's official Python library
(stripe==16.0.0, offline `WebhookSignature.verify_header`) was observed to do. Signatures
come from the simulator's own signing code, never from the library.

The committed file must equal `render()` byte for byte; a test enforces this.
Run: python tools/make_vectors.py
"""

import base64
import json
import random
from pathlib import Path

from simulate import build_event, serialize, sign

OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "signature_vectors.json"

SECRET = "whsec_test_fake_vector_secret"
OTHER_SECRET = "whsec_test_fake_vector_other_secret"
NOW = 1_790_000_000
TOLERANCE = 300

# Outcomes. Ours: "valid" or the SignatureError subclass name.
# Library: "accepted" or the exception class name it raised.
VALID, ACCEPTED, SVE = "valid", "accepted", "SignatureVerificationError"


def _case(name, body, header, ours, library, difference=None, *, now=NOW):
    return {
        "name": name,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "header": header,
        "now": now,
        "tolerance": TOLERANCE,
        "ours": ours,
        "library": library,
        "difference": difference,
    }


def build_cases() -> list[dict]:
    body = serialize(build_event(random.Random(2026), created=NOW))
    empty = b""
    non_utf8 = b'{"id":"evt_test_bytes","note":"\xff\xfe\x80"}'

    def v1(t, b=body, secret=SECRET):
        return sign(t, b, secret)

    good = f"t={NOW},v1={v1(NOW)}"
    arabic_now = "".join(chr(0x660 + int(d)) for d in str(NOW))

    return [
        # --- both accept -----------------------------------------------------------
        _case("valid", body, good, VALID, ACCEPTED),
        _case("valid_with_v0", body, f"{good},v0={v1(NOW, secret='fake_v0')}", VALID, ACCEPTED),
        _case(
            "rolled_secret_current_last",
            body,
            f"t={NOW},v1={v1(NOW, secret=OTHER_SECRET)},v1={v1(NOW)}",
            VALID,
            ACCEPTED,
        ),
        _case(
            "rolled_secret_current_first",
            body,
            f"t={NOW},v1={v1(NOW)},v1={v1(NOW, secret=OTHER_SECRET)}",
            VALID,
            ACCEPTED,
        ),
        _case("empty_body", empty, f"t={NOW},v1={v1(NOW, empty)}", VALID, ACCEPTED),
        _case(
            "past_at_tolerance_edge",
            body,
            f"t={NOW - TOLERANCE},v1={v1(NOW - TOLERANCE)}",
            VALID,
            ACCEPTED,
        ),
        _case("future_small_clock_skew", body, f"t={NOW + 5},v1={v1(NOW + 5)}", VALID, ACCEPTED),
        _case(
            "future_at_tolerance_edge",
            body,
            f"t={NOW + TOLERANCE},v1={v1(NOW + TOLERANCE)}",
            VALID,
            ACCEPTED,
        ),
        # --- both reject -----------------------------------------------------------
        _case("tampered_body", body + b" ", good, "SignatureMismatchError", SVE),
        _case(
            "wrong_secret",
            body,
            f"t={NOW},v1={v1(NOW, secret=OTHER_SECRET)}",
            "SignatureMismatchError",
            SVE,
        ),
        _case(
            "timestamp_changed_after_signing",
            body,
            f"t={NOW + 1},v1={v1(NOW)}",
            "SignatureMismatchError",
            SVE,
        ),
        _case(
            "expired",
            body,
            f"t={NOW - TOLERANCE - 1},v1={v1(NOW - TOLERANCE - 1)}",
            "TimestampOutsideToleranceError",
            SVE,
        ),
        _case("v0_only", body, f"t={NOW},v0={v1(NOW)}", "NoV1SignatureError", SVE),
        _case("missing_header", body, "", "MissingHeaderError", SVE),
        _case("non_integer_timestamp", body, f"t=abc,v1={v1(NOW)}", "MalformedHeaderError", SVE),
        _case("negative_timestamp", body, f"t=-5,v1={v1(-5)}", "MalformedHeaderError", SVE),
        # --- known differences -----------------------------------------------------
        _case(
            "future_beyond_tolerance",
            body,
            f"t={NOW + TOLERANCE + 1},v1={v1(NOW + TOLERANCE + 1)}",
            "TimestampOutsideToleranceError",
            ACCEPTED,
            "The library only rejects timestamps that are too old; we also reject ones too "
            "far in the future.",
        ),
        _case(
            "non_utf8_body",
            non_utf8,
            f"t={NOW},v1={v1(NOW, non_utf8)}",
            VALID,
            "UnicodeDecodeError",
            "The library decodes the body as UTF-8 before signing; we sign raw bytes.",
        ),
        _case(
            "non_ascii_v1",
            body,
            f"t={NOW},v1={'é' * 64}",
            "SignatureMismatchError",
            "TypeError",
            "The library compares str values with hmac.compare_digest, which raises on "
            "non-ASCII input; we compare bytes.",
        ),
        _case(
            "timestamp_with_underscores",
            body,
            f"t=1_790_000_000,v1={v1(NOW)}",
            "MalformedHeaderError",
            ACCEPTED,
            "The library parses t with int(), which accepts underscores; we require ASCII "
            "digits only.",
        ),
        _case(
            "timestamp_with_plus_sign",
            body,
            f"t=+{NOW},v1={v1(NOW)}",
            "MalformedHeaderError",
            ACCEPTED,
            "int() accepts a leading '+'; we require ASCII digits only.",
        ),
        _case(
            "timestamp_with_leading_space",
            body,
            f"t= {NOW},v1={v1(NOW)}",
            "MalformedHeaderError",
            ACCEPTED,
            "int() strips whitespace; we require ASCII digits only.",
        ),
        _case(
            "timestamp_non_ascii_digits",
            body,
            f"t={arabic_now},v1={v1(NOW)}",
            "MalformedHeaderError",
            ACCEPTED,
            "int() accepts Unicode digits such as Arabic-Indic; we require ASCII digits only.",
        ),
        _case(
            "duplicate_timestamp",
            body,
            f"t={NOW},t={NOW - 9999},v1={v1(NOW)}",
            "MalformedHeaderError",
            ACCEPTED,
            "The library uses the first t and ignores the rest; we require exactly one.",
        ),
        _case(
            "item_without_equals",
            body,
            f"{good},junk",
            "MalformedHeaderError",
            ACCEPTED,
            "The library ignores items without '='; we treat them as malformed.",
        ),
    ]


def render() -> bytes:
    document = {
        "description": (
            "Signature test vectors for IntegrationKit. All data is fake. Generated by "
            "tools/make_vectors.py; do not edit by hand. 'ours' is the required result of "
            "integrationkit.signature.verify_signature; 'library' is the observed result of "
            "stripe==16.0.0 WebhookSignature.verify_header with the same tolerance and a "
            "clock fixed at 'now'."
        ),
        "secret": SECRET,
        "cases": build_cases(),
    }
    return (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode("ascii")


if __name__ == "__main__":
    OUTPUT.write_bytes(render())
    print(f"wrote {OUTPUT}")
