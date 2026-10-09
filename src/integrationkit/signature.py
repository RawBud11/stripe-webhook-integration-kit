"""Verification of Stripe-style webhook signatures.

Follows Stripe's documented manual scheme (https://docs.stripe.com/webhooks, "Verify
webhook signatures manually"):

- The `Stripe-Signature` header holds `t=<unix timestamp>` and one or more `v1=<hex>`.
  Other schemes (including the `v0` Stripe adds to test events) are ignored.
- The signature is hex HMAC-SHA256 of `<t>.<raw body>`, keyed with the endpoint secret.
- Any matching `v1` passes (several are present while a secret is being rolled).
- Comparison is constant-time, and the timestamp must be within a tolerance.

Error messages never contain the secret or any signature value.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass

EXPECTED_SCHEME = "v1"
_TIMESTAMP_RE = re.compile(r"[0-9]+")


class SignatureError(Exception):
    """Base class for every reason a request fails verification."""


class MissingHeaderError(SignatureError):
    pass


class MalformedHeaderError(SignatureError):
    pass


class NoV1SignatureError(SignatureError):
    pass


class SignatureMismatchError(SignatureError):
    pass


class TimestampOutsideToleranceError(SignatureError):
    pass


@dataclass(frozen=True)
class ParsedHeader:
    timestamp: int
    v1_signatures: tuple[str, ...]


def parse_header(header: str | None) -> ParsedHeader:
    """Extract the timestamp and every v1 signature; discard all other schemes."""
    if header is None or not header.strip():
        raise MissingHeaderError("Stripe-Signature header is missing or empty")

    timestamps: list[str] = []
    signatures: list[str] = []
    for item in header.split(","):
        prefix, sep, value = item.strip().partition("=")
        if not sep:
            raise MalformedHeaderError("header item without '='")
        if prefix == "t":
            timestamps.append(value)
        elif prefix == EXPECTED_SCHEME:
            signatures.append(value)

    if len(timestamps) != 1:
        raise MalformedHeaderError("header must contain exactly one timestamp")
    if not _TIMESTAMP_RE.fullmatch(timestamps[0]):
        raise MalformedHeaderError("timestamp is not a non-negative integer")
    if not signatures:
        raise NoV1SignatureError("no v1 signature in header")

    return ParsedHeader(timestamp=int(timestamps[0]), v1_signatures=tuple(signatures))


def compute_signature(timestamp: int, raw_body: bytes, secret: str | bytes) -> str:
    """Hex HMAC-SHA256 of `<timestamp>.<raw body>`. The body is used as raw bytes."""
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    signed_payload = f"{timestamp}.".encode("ascii") + raw_body
    return hmac.new(key, signed_payload, hashlib.sha256).hexdigest()


def verify_signature(
    raw_body: bytes,
    header: str | None,
    secret: str | bytes,
    *,
    now: int,
    tolerance: int,
) -> int:
    """Verify a webhook request and return its signature timestamp.

    Raises a SignatureError subclass on failure. `now` is passed in so tests control the
    clock. A tolerance of 0 would disable the recency check, so it is refused.
    """
    if tolerance <= 0:
        raise ValueError("tolerance must be greater than 0")

    parsed = parse_header(header)
    expected = compute_signature(parsed.timestamp, raw_body, secret).encode("ascii")

    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would
    # otherwise surface as a server error instead of a rejected request.
    if not any(
        hmac.compare_digest(expected, candidate.encode("utf-8", errors="replace"))
        for candidate in parsed.v1_signatures
    ):
        raise SignatureMismatchError("no v1 signature matches the expected signature")

    if abs(now - parsed.timestamp) > tolerance:
        raise TimestampOutsideToleranceError("signature timestamp is outside the tolerance")

    return parsed.timestamp
