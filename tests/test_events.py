"""Parsing and validation of verified bodies. No database needed."""

import json

import pytest

from integrationkit.events import (
    NUL_ERROR,
    NUMBER_ERROR,
    SURROGATE_ERROR,
    InvalidEventError,
    jsonb_problem,
    parse_event,
)


def event(**overrides) -> dict:
    base = {
        "id": "evt_test_0123456789abcdef01234567",
        "object": "event",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {"id": "cs_test_a11YYufWQzNY63zpQ6QSNRQhkUpVph4WRmzW0zWJO2znZKdVujZ0N0S22u"}
        },
    }
    base.update(overrides)
    return base


def body(value) -> bytes:
    return json.dumps(value).encode()


def test_checkout_event_gets_session_id():
    parsed = parse_event(body(event()))
    assert parsed.event_id == "evt_test_0123456789abcdef01234567"
    assert parsed.event_type == "checkout.session.completed"
    assert parsed.livemode is False
    assert parsed.session_id.startswith("cs_test_a11YY")
    assert parsed.payload == event()
    assert parsed.payload_error is None


@pytest.mark.parametrize(
    "event_type, object_id",
    [
        ("invoice.paid", "in_test_123"),
        ("payment_intent.succeeded", "pi_test_123"),
        ("customer.subscription.created", "sub_test_123"),
        ("invoice.paid", "cs_test_looks_like_a_session"),  # not a checkout.session.* type
    ],
)
def test_non_checkout_types_are_accepted_without_session_id(event_type, object_id):
    parsed = parse_event(body(event(type=event_type, data={"object": {"id": object_id}})))
    assert parsed.event_type == event_type
    assert parsed.session_id is None


@pytest.mark.parametrize(
    "value",
    [
        {"id": "evt_test_1", "type": "x.y", "livemode": False},  # no data at all
        event(data={"object": {"id": "in_test_123"}}),  # wrong prefix
        event(data={"object": {}}),
        event(data={"object": "cs_test_123"}),
        event(data=[]),
        event(data={"object": {"id": 123}}),
        event(data={"object": {"id": "cs_test_\u0000"}}),
    ],
)
def test_session_id_is_none_when_object_id_unusable(value):
    assert parse_event(body(value)).session_id is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[1, 2]", id="array"),
        pytest.param(b'"evt_test_1"', id="string"),
        pytest.param(b"null", id="null"),
        pytest.param(b'{"id": "evt_test_1", "type": "x.y", "livemode": NaN}', id="nan"),
        pytest.param(
            b'{"id": "evt_test_1", "type": "x.y", "livemode": false, "n": Infinity}',
            id="infinity",
        ),
        pytest.param(b"\xff\xfe{}", id="not-utf8"),
        # Nesting deeper than the parser allows raises RecursionError internally.
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="deep-nesting"),
    ],
)
def test_invalid_json_is_rejected(raw):
    with pytest.raises(InvalidEventError):
        parse_event(raw)


@pytest.mark.parametrize(
    "event_id",
    [
        None,
        123,
        "",
        "evt_",
        "evt_test-1",
        "evt_test_1\u0000",
        "xyz_test_1",
        "evt_" + "a" * 251,
        "evt_tést",
        " evt_test_1",
    ],
)
def test_malformed_event_id_rejected(event_id):
    with pytest.raises(InvalidEventError, match="event id"):
        parse_event(body(event(id=event_id)))


@pytest.mark.parametrize(
    "event_type",
    [
        None,
        1,
        "",
        "checkout",
        "Checkout.Session.Completed",
        "a..b",
        ".a.b",
        "a.b.",
        "a b.c",
        "a.b\u0000",
    ],
)
def test_malformed_event_type_rejected(event_type):
    with pytest.raises(InvalidEventError, match="event type"):
        parse_event(body(event(type=event_type)))


@pytest.mark.parametrize("event_type", ["v1.billing.meter.error_report_triggered", "a.b"])
def test_other_well_formed_types_accepted(event_type):
    assert parse_event(body(event(type=event_type))).event_type == event_type


@pytest.mark.parametrize("livemode", [None, "false", 0, 1])
def test_livemode_must_be_boolean(livemode):
    with pytest.raises(InvalidEventError, match="livemode"):
        parse_event(body(event(livemode=livemode)))


def test_missing_livemode_rejected():
    value = event()
    del value["livemode"]
    with pytest.raises(InvalidEventError, match="livemode"):
        parse_event(body(value))


def test_livemode_true_is_parsed_not_rejected_here():
    assert parse_event(body(event(livemode=True))).livemode is True


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"name": "Test\u0000Buyer"}, NUL_ERROR),
        ({"key\u0000": "v"}, NUL_ERROR),
        ({"list": ["ok", ["a\u0000"]]}, NUL_ERROR),
        ({"name": "\ud800"}, SURROGATE_ERROR),
    ],
)
def test_jsonb_problems_detected(value, expected):
    parsed = parse_event(body(event(data={"object": value})))
    assert parsed.payload is None
    assert parsed.payload_error == expected


def test_number_beyond_float_range_is_a_payload_error():
    # Valid JSON (jsonb's numeric could hold it), but Python parses it to float('inf').
    raw = b'{"id": "evt_test_1", "type": "a.b", "livemode": false, "n": 1e400}'
    parsed = parse_event(raw)
    assert parsed.payload is None
    assert parsed.payload_error == NUMBER_ERROR


def test_valid_surrogate_pair_is_fine():
    raw = b'{"id": "evt_test_1", "type": "a.b", "livemode": false, "e": "\\ud83d\\ude00"}'
    parsed = parse_event(raw)
    assert parsed.payload_error is None
    assert parsed.payload["e"] == "\U0001f600"


def test_jsonb_problem_none_for_normal_values():
    assert jsonb_problem({"a": [1, 2.5, True, None, "text", {"b": "c"}], "big": 10**40}) is None


def test_error_messages_contain_no_payload_content():
    secretish = "buyer-private@example.com"
    for raw in (
        body(event(id=secretish)),
        body(event(type=secretish)),
        body(event(livemode=secretish)),
        secretish.encode(),
    ):
        with pytest.raises(InvalidEventError) as exc_info:
            parse_event(raw)
        assert secretish not in str(exc_info.value)
