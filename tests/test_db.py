"""Migration and storage tests against a real PostgreSQL test database."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from integrationkit.models import Base, WebhookEvent

pytestmark = pytest.mark.db

SIGNED_AT = datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)


def make_event(**overrides) -> WebhookEvent:
    values = {
        "event_id": "evt_test_000000000000000000000001",
        "event_type": "checkout.session.completed",
        "session_id": "cs_test_000000000000000000000001",
        "livemode": False,
        "raw_body": b'{"id":"evt_test_000000000000000000000001"}',
        "payload": {"id": "evt_test_000000000000000000000001"},
        "signature_ts": SIGNED_AT,
    }
    values.update(overrides)
    return WebhookEvent(**values)


def store(engine, *events) -> None:
    with Session(engine) as session, session.begin():
        session.add_all(events)


# --- migrations ---------------------------------------------------------------------


def test_upgrade_creates_and_downgrade_drops(db_engine, alembic_config):
    command.downgrade(alembic_config, "base")
    assert "webhook_events" not in inspect(db_engine).get_table_names()

    command.upgrade(alembic_config, "head")
    inspector = inspect(db_engine)
    assert "webhook_events" in inspector.get_table_names()
    indexes = {ix["name"]: ix for ix in inspector.get_indexes("webhook_events")}
    assert indexes["ix_webhook_events_session_id"]["unique"] is False

    command.downgrade(alembic_config, "base")
    assert "webhook_events" not in inspect(db_engine).get_table_names()


def test_models_match_migrations(migrated_db):
    with migrated_db.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


# --- storage ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{\n  "id": "evt_test_1",\n  "b":   2\n}\n',  # whitespace kept exactly
        b'{"note":"\xff\xfe\x80 not utf-8"}',  # non-UTF-8 bytes
        b'{"note":"a\\u0000b"}',  # a JSON \u0000 escape, as raw bytes
        b"",
    ],
)
def test_raw_body_round_trips_byte_for_byte(migrated_db, raw_body):
    store(migrated_db, make_event(raw_body=raw_body))
    with Session(migrated_db) as session:
        assert session.scalars(select(WebhookEvent.raw_body)).one() == raw_body


def test_event_id_is_unique(migrated_db):
    store(migrated_db, make_event())
    with pytest.raises(IntegrityError):
        store(migrated_db, make_event(session_id="cs_test_other"))


def test_session_id_is_not_unique(migrated_db):
    store(
        migrated_db,
        make_event(event_id="evt_test_a"),
        make_event(event_id="evt_test_b"),
    )
    with Session(migrated_db) as session:
        assert len(session.scalars(select(WebhookEvent)).all()) == 2


def test_timestamps_are_timezone_aware_utc(migrated_db):
    before = datetime.now(UTC)
    store(migrated_db, make_event())
    after = datetime.now(UTC)
    with Session(migrated_db) as session:
        event = session.scalars(select(WebhookEvent)).one()

    assert event.signature_ts == SIGNED_AT
    assert event.signature_ts.utcoffset() == timedelta(0)
    # Set by the database default, not by Python.
    assert event.received_at.utcoffset() == timedelta(0)
    assert before - timedelta(seconds=5) <= event.received_at <= after + timedelta(seconds=5)


def test_non_utc_signature_ts_is_stored_as_the_same_instant(migrated_db):
    minus_five = SIGNED_AT.astimezone(timezone(timedelta(hours=-5)))
    store(migrated_db, make_event(signature_ts=minus_five))
    with Session(migrated_db) as session:
        stored = session.scalars(select(WebhookEvent.signature_ts)).one()
    assert stored == SIGNED_AT
    assert stored.utcoffset() == timedelta(0)


# --- \u0000 behaviour (documents PostgreSQL, decides step 6) ------------------------


def test_jsonb_rejects_null_character(migrated_db):
    """PostgreSQL docs: jsonb rejects \\u0000 because text cannot hold it."""
    with pytest.raises(DataError) as exc_info:
        store(migrated_db, make_event(payload={"note": "a\u0000b"}))
    assert exc_info.value.orig.sqlstate == "22P05"  # untranslatable_character


def test_text_column_rejects_null_character(migrated_db):
    """psycopg refuses NUL in text parameters before anything reaches the server."""
    with pytest.raises(DataError, match="cannot contain NUL"):
        store(migrated_db, make_event(session_id="cs_test_\u0000"))
