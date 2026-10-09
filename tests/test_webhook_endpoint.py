"""POST /webhooks/stripe end to end, against the PostgreSQL test database."""

import json
import logging
import random

import pytest
import simulate
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from integrationkit.app import create_app
from integrationkit.db import make_engine
from integrationkit.events import NUL_ERROR
from integrationkit.models import WebhookEvent
from integrationkit.settings import Settings

SECRET = "whsec_test_fake_endpoint_secret"
NOW = 1_790_000_000
MAX_BODY = 4096
URL = "/webhooks/stripe"
LOGGER = "integrationkit.webhooks"
GENERIC_400 = {"error": "invalid request"}


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        webhook_secret=SECRET,
        database_url="postgresql+psycopg://unused@127.0.0.1:1/unused_test",
        max_body_bytes=MAX_BODY,
    )


def client_for(engine) -> TestClient:
    app = create_app(settings=make_settings(), engine=engine, clock=lambda: NOW)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(migrated_db):
    with client_for(migrated_db) as test_client:
        yield test_client


def simulated(variant=None, seed=1, **kwargs):
    return simulate.build_requests(
        secret=SECRET, now=NOW, rng=random.Random(seed), variant=variant, **kwargs
    )


def signed(value: dict | bytes, t: int = NOW) -> tuple[bytes, dict[str, str]]:
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    header = simulate.signature_header(t, [simulate.sign(t, raw, SECRET)])
    return raw, {"Stripe-Signature": header, "Content-Type": "application/json"}


def post(client, request: simulate.SimulatedRequest):
    return client.post(URL, content=request.body, headers=request.headers)


def stored(engine) -> list[WebhookEvent]:
    with Session(engine) as session:
        return list(session.scalars(select(WebhookEvent).order_by(WebhookEvent.id)))


def count(engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(WebhookEvent))


def base_event(**overrides) -> dict:
    event = json.loads(simulated()[0].body)
    event.update(overrides)
    return event


# --- happy path ---------------------------------------------------------------------


@pytest.mark.db
def test_valid_event_is_stored_exactly(client, migrated_db):
    (request,) = simulated()
    response = post(client, request)

    assert response.status_code == 200
    assert response.json() == {"received": True}
    (row,) = stored(migrated_db)
    payload = json.loads(request.body)
    assert row.raw_body == request.body
    assert row.payload == payload
    assert row.payload_error is None
    assert row.event_id == payload["id"]
    assert row.event_type == "checkout.session.completed"
    assert row.session_id == payload["data"]["object"]["id"]
    assert row.livemode is False
    assert int(row.signature_ts.timestamp()) == NOW


@pytest.mark.db
@pytest.mark.parametrize(
    "variant, kwargs",
    [
        ("rolled_secret", {}),
        (None, {"with_v0": True}),
        (None, {"payment_status": "unpaid"}),
    ],
)
def test_accepted_simulator_variants(client, migrated_db, variant, kwargs):
    (request,) = simulated(variant, **kwargs)
    assert post(client, request).status_code == 200
    assert count(migrated_db) == 1


@pytest.mark.db
def test_duplicate_stores_one_row_and_returns_200_twice(client, migrated_db, caplog):
    first, second = simulated("duplicate")
    caplog.set_level(logging.INFO, logger=LOGGER)

    assert post(client, first).status_code == 200
    assert post(client, second).status_code == 200

    (row,) = stored(migrated_db)
    assert row.raw_body == first.body
    assert int(row.signature_ts.timestamp()) == NOW - 60  # first delivery wins
    assert any(r.getMessage().startswith("duplicate event") for r in caplog.records)


@pytest.mark.db
@pytest.mark.parametrize(
    "event_type, object_id",
    [("invoice.paid", "in_test_123"), ("payment_intent.succeeded", "pi_test_123")],
)
def test_non_checkout_event_is_stored_without_session_id(
    client, migrated_db, event_type, object_id
):
    raw, headers = signed(base_event(type=event_type, data={"object": {"id": object_id}}))
    assert client.post(URL, content=raw, headers=headers).status_code == 200
    (row,) = stored(migrated_db)
    assert row.event_type == event_type
    assert row.session_id is None
    assert row.raw_body == raw


# --- rejected before storage --------------------------------------------------------


@pytest.mark.db
@pytest.mark.parametrize(
    "variant",
    ["tamper_body", "wrong_secret", "expired", "missing_header", "malformed_header"],
)
def test_signature_failures_return_generic_400(client, migrated_db, variant):
    (request,) = simulated(variant)
    response = post(client, request)
    assert response.status_code == 400
    assert response.json() == GENERIC_400
    assert count(migrated_db) == 0


@pytest.mark.db
@pytest.mark.parametrize(
    "value",
    [
        b"not json",
        b"[]",
        {"id": "not-an-event-id", "type": "a.b", "livemode": False},
        {"id": "evt_test_1", "type": "Not A Type", "livemode": False},
        {"id": "evt_test_1", "type": "a.b"},  # no livemode
        {"id": "evt_test_1\u0000", "type": "a.b", "livemode": False},
    ],
)
def test_verified_but_unidentifiable_events_return_generic_400(client, migrated_db, value):
    raw, headers = signed(value)
    response = client.post(URL, content=raw, headers=headers)
    assert response.status_code == 400
    assert response.json() == GENERIC_400
    assert count(migrated_db) == 0


# --- livemode -----------------------------------------------------------------------


@pytest.mark.db
def test_livemode_event_is_refused_with_422_and_minimal_log(client, migrated_db, caplog):
    event = base_event(livemode=True)
    customer = event["data"]["object"]["customer_details"]
    raw, headers = signed(event)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    response = client.post(URL, content=raw, headers=headers)

    assert response.status_code == 422
    assert response.json() == {"error": "unprocessable event"}
    assert count(migrated_db) == 0

    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert event["id"] in message and event["type"] in message
    for record in caplog.records:
        text = record.getMessage()
        for forbidden in (
            customer["email"],
            customer["name"],
            event["data"]["object"]["id"],
            event["data"]["object"]["client_reference_id"],
            raw.decode(),
            SECRET,
        ):
            assert forbidden not in text


# --- payloads jsonb cannot hold -----------------------------------------------------


@pytest.mark.db
def test_nul_in_payload_is_stored_with_payload_error(client, migrated_db):
    event = base_event()
    event["data"]["object"]["customer_details"]["name"] = "Test\u0000Buyer"
    raw, headers = signed(event)
    assert b"\\u0000" in raw

    response = client.post(URL, content=raw, headers=headers)

    assert response.status_code == 200
    (row,) = stored(migrated_db)
    assert row.raw_body == raw
    assert row.payload is None
    assert row.payload_error == NUL_ERROR
    assert row.session_id == event["data"]["object"]["id"]


@pytest.mark.db
def test_unpaired_surrogate_in_payload_is_stored_with_payload_error(client, migrated_db):
    event = base_event()
    event["data"]["object"]["metadata"]["note"] = "\ud800"
    raw, headers = signed(event)
    assert client.post(URL, content=raw, headers=headers).status_code == 200
    (row,) = stored(migrated_db)
    assert row.payload is None and row.payload_error is not None


# --- body size ----------------------------------------------------------------------


def padded_event(size: int) -> bytes:
    event = base_event()
    event["data"]["object"]["metadata"]["pad"] = ""
    base_len = len(json.dumps(event).encode())
    event["data"]["object"]["metadata"]["pad"] = "x" * (size - base_len)
    raw = json.dumps(event).encode()
    assert len(raw) == size
    return raw


@pytest.mark.db
def test_body_exactly_at_limit_is_accepted(client, migrated_db):
    raw, headers = signed(padded_event(MAX_BODY))
    assert client.post(URL, content=raw, headers=headers).status_code == 200


@pytest.mark.db
def test_body_over_limit_with_content_length_returns_413(client, migrated_db):
    raw, headers = signed(padded_event(MAX_BODY + 1))
    response = client.post(URL, content=raw, headers=headers)
    assert response.status_code == 413
    assert response.json() == {"error": "payload too large"}
    assert count(migrated_db) == 0


@pytest.mark.db
def test_body_over_limit_without_content_length_returns_413(client, migrated_db):
    raw, headers = signed(padded_event(MAX_BODY + 1))

    def chunks():
        for i in range(0, len(raw), 1000):
            yield raw[i : i + 1000]

    response = client.post(URL, content=chunks(), headers=headers)
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413
    assert count(migrated_db) == 0


# --- failures inside our service ----------------------------------------------------


def test_database_outage_returns_503():
    engine = make_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/unreachable_test")
    with client_for(engine) as client:
        response = post(client, simulated()[0])
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily unavailable"}


@pytest.mark.db
def test_unexpected_exception_returns_generic_500(migrated_db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr("integrationkit.app.store_event", boom)
    with client_for(migrated_db) as client:
        response = post(client, simulated()[0])
    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}
    assert "internal detail" not in response.text


@pytest.mark.db
def test_non_outage_database_error_returns_500_not_503(migrated_db):
    with migrated_db.begin() as connection:
        connection.exec_driver_sql("DROP TABLE webhook_events")
    with client_for(migrated_db) as client:
        response = post(client, simulated()[0])
    assert response.status_code == 500
    assert response.json() == {"error": "internal error"}


def test_only_post_is_allowed():
    engine = make_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/unreachable_test")
    with client_for(engine) as client:
        assert client.get(URL).status_code == 405
        assert client.get("/docs").status_code == 404


@pytest.mark.db
@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"object": {"id": "not_a_cs_id_private_value"}}, id="malformed"),
        pytest.param({"object": {"customer_email": "private@example.com"}}, id="missing"),
        pytest.param({}, id="no-object"),
    ],
)
def test_checkout_event_without_usable_session_id_is_stored_with_warning(
    client, migrated_db, caplog, data
):
    event = base_event(data=data)
    raw, headers = signed(event)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    assert client.post(URL, content=raw, headers=headers).status_code == 200

    (row,) = stored(migrated_db)
    assert row.session_id is None
    assert row.raw_body == raw
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert event["id"] in message and event["type"] in message
    for record in caplog.records:
        text = record.getMessage()
        assert "private" not in text
        assert raw.decode() not in text


@pytest.mark.db
def test_valid_checkout_event_logs_no_warning(client, migrated_db, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    assert post(client, simulated()[0]).status_code == 200
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
