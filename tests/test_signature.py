import hashlib
import hmac

import pytest

from integrationkit.signature import (
    MalformedHeaderError,
    MissingHeaderError,
    NoV1SignatureError,
    SignatureError,
    SignatureMismatchError,
    TimestampOutsideToleranceError,
    compute_signature,
    parse_header,
    verify_signature,
)

SECRET = "whsec_test_fake_signature_secret"
OLD_SECRET = "whsec_test_fake_previous_secret"
NOW = 1_790_000_000
TOLERANCE = 300
BODY = b'{"id":"evt_test_123","object":"event","type":"checkout.session.completed"}'


def header_for(body: bytes, *secrets: str, t: int = NOW, extra: str = "") -> str:
    parts = [f"t={t}"] + [f"v1={compute_signature(t, body, s)}" for s in secrets]
    if extra:
        parts.append(extra)
    return ",".join(parts)


def verify(body: bytes, header: str | None, secret: str | bytes = SECRET, **kwargs) -> int:
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("tolerance", TOLERANCE)
    return verify_signature(body, header, secret, **kwargs)


# --- compute_signature ------------------------------------------------------------


def test_compute_signature_matches_hand_built_hmac():
    expected = hmac.new(
        SECRET.encode("utf-8"), f"{NOW}.".encode() + BODY, hashlib.sha256
    ).hexdigest()
    assert compute_signature(NOW, BODY, SECRET) == expected


def test_compute_signature_accepts_bytes_secret():
    assert compute_signature(NOW, BODY, SECRET.encode()) == compute_signature(NOW, BODY, SECRET)


# --- valid requests ---------------------------------------------------------------


def test_valid_signature_returns_timestamp():
    assert verify(BODY, header_for(BODY, SECRET)) == NOW


def test_empty_body_signed_and_verified():
    assert verify(b"", header_for(b"", SECRET)) == NOW


def test_non_utf8_body_signed_and_verified():
    body = b'{"note":"\xff\xfe\x80 not utf-8"}'
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")
    assert verify(body, header_for(body, SECRET)) == NOW


@pytest.mark.parametrize("order", [(OLD_SECRET, SECRET), (SECRET, OLD_SECRET)])
def test_rolled_secret_any_v1_matches(order):
    assert verify(BODY, header_for(BODY, *order)) == NOW


def test_v0_and_unknown_schemes_are_ignored():
    header = header_for(BODY, SECRET, extra="v0=deadbeef,v2=cafe")
    assert verify(BODY, header) == NOW


def test_timestamp_exactly_at_tolerance_is_accepted():
    header = header_for(BODY, SECRET, t=NOW - TOLERANCE)
    assert verify(BODY, header) == NOW - TOLERANCE


# --- signature failures -----------------------------------------------------------


def test_tampered_body_fails():
    header = header_for(BODY, SECRET)
    with pytest.raises(SignatureMismatchError):
        verify(BODY.replace(b"evt_test_123", b"evt_test_999"), header)


def test_single_byte_change_fails():
    header = header_for(BODY, SECRET)
    with pytest.raises(SignatureMismatchError):
        verify(BODY + b" ", header)


def test_wrong_secret_fails():
    with pytest.raises(SignatureMismatchError):
        verify(BODY, header_for(BODY, "whsec_test_fake_wrong_secret"))


def test_timestamp_changed_after_signing_fails():
    signature = compute_signature(NOW, BODY, SECRET)
    with pytest.raises(SignatureMismatchError):
        verify(BODY, f"t={NOW + 1},v1={signature}")


def test_only_v0_signature_fails_even_if_it_would_match():
    signature = compute_signature(NOW, BODY, SECRET)
    with pytest.raises(NoV1SignatureError):
        verify(BODY, f"t={NOW},v0={signature}")


@pytest.mark.parametrize("value", ["é" * 64, "签名", "\u0000abc", ""])
def test_non_ascii_or_empty_v1_is_a_mismatch_not_a_crash(value):
    with pytest.raises(SignatureMismatchError):
        verify(BODY, f"t={NOW},v1={value}")


# --- timestamp failures -----------------------------------------------------------


def test_expired_timestamp_fails():
    header = header_for(BODY, SECRET, t=NOW - TOLERANCE - 1)
    with pytest.raises(TimestampOutsideToleranceError):
        verify(BODY, header)


def test_future_timestamp_fails():
    header = header_for(BODY, SECRET, t=NOW + TOLERANCE + 1)
    with pytest.raises(TimestampOutsideToleranceError):
        verify(BODY, header)


@pytest.mark.parametrize("tolerance", [0, -1])
def test_non_positive_tolerance_refused(tolerance):
    with pytest.raises(ValueError):
        verify(BODY, header_for(BODY, SECRET), tolerance=tolerance)


# --- header problems --------------------------------------------------------------


@pytest.mark.parametrize("header", [None, "", "   "])
def test_missing_or_empty_header(header):
    with pytest.raises(MissingHeaderError):
        verify(BODY, header)


@pytest.mark.parametrize(
    "header",
    [
        "garbage",
        "v1=abc",  # no timestamp
        "t=,v1=abc",  # empty timestamp
        "t=abc,v1=abc",  # non-integer timestamp
        "t=-5,v1=abc",  # negative
        "t=1_000,v1=abc",  # int() would accept this
        "t=+100,v1=abc",  # int() would accept this
        "t=١٢٣,v1=abc",  # non-ASCII digits, int() would accept these
        "t=1,t=2,v1=abc",  # duplicate timestamp
        f"t={NOW},v1abc",  # item without '='
    ],
)
def test_malformed_header(header):
    with pytest.raises(MalformedHeaderError):
        verify(BODY, header)


def test_header_without_v1():
    with pytest.raises(NoV1SignatureError):
        verify(BODY, f"t={NOW}")


def test_parse_header_splits_on_first_equals_only():
    parsed = parse_header("t=1,v1=a=b,v1=c")
    assert parsed.timestamp == 1
    assert parsed.v1_signatures == ("a=b", "c")


def test_every_failure_is_a_signature_error():
    for exc in (
        MissingHeaderError,
        MalformedHeaderError,
        NoV1SignatureError,
        SignatureMismatchError,
        TimestampOutsideToleranceError,
    ):
        assert issubclass(exc, SignatureError)


# --- no secret material in errors ---------------------------------------------------


@pytest.mark.parametrize(
    "body, t, header",
    [
        (BODY + b"x", NOW, header_for(BODY, SECRET)),  # mismatch
        (BODY, NOW, header_for(BODY, "whsec_test_fake_other")),  # wrong secret
        (BODY, NOW - 10_000, header_for(BODY, SECRET, t=NOW - 10_000)),  # expired
        (BODY, NOW, f"t={NOW}"),  # no v1
        (BODY, NOW, f"t=x,v1={compute_signature(NOW, BODY, SECRET)}"),  # malformed
    ],
)
def test_errors_do_not_leak_secret_or_signatures(body, t, header):
    with pytest.raises(SignatureError) as exc_info:
        verify(body, header)

    text = " ".join([str(exc_info.value), repr(exc_info.value), *map(str, exc_info.value.args)])
    received = [item[3:] for item in header.split(",") if item.startswith("v1=")]
    forbidden = [SECRET, compute_signature(t, body, SECRET), *received]
    for value in forbidden:
        assert value not in text
