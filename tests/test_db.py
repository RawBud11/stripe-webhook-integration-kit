"""Migration and storage tests against a real PostgreSQL test database."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from db_support import ROOT
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


def test_upgrade_creates_and_downgrade_drops(migrated_db, alembic_config):
    command.downgrade(alembic_config, "base")
    assert "webhook_events" not in inspect(migrated_db).get_table_names()

    command.upgrade(alembic_config, "head")
    inspector = inspect(migrated_db)
    assert "webhook_events" in inspector.get_table_names()
    indexes = {ix["name"]: ix for ix in inspector.get_indexes("webhook_events")}
    assert indexes["ix_webhook_events_session_id"]["unique"] is False

    command.downgrade(alembic_config, "base")
    assert "webhook_events" not in inspect(migrated_db).get_table_names()


def test_each_migration_steps_down_and_up(migrated_db, alembic_config):
    command.downgrade(alembic_config, "0001")
    columns = {c["name"]: c for c in inspect(migrated_db).get_columns("webhook_events")}
    assert "payload_error" not in columns
    assert columns["payload"]["nullable"] is False

    command.upgrade(alembic_config, "0002")
    columns = {c["name"]: c for c in inspect(migrated_db).get_columns("webhook_events")}
    assert columns["payload"]["nullable"] is True
    checks = inspect(migrated_db).get_check_constraints("webhook_events")
    assert [c["name"] for c in checks] == ["ck_webhook_events_payload_xor_error"]


def test_alembic_cli_logs_each_migration(migrated_db, alembic_config, test_database_url):
    # The container runs this CLI at start; its log is the only record of what was applied.
    command.downgrade(alembic_config, "base")
    env = {
        **os.environ,
        "DATABASE_URL": test_database_url,
        "WEBHOOK_SECRET": "whsec_test_fake_alembic_cli",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "INFO  [alembic.runtime.migration] Running upgrade  -> 0001" in result.stderr
    assert "Running upgrade 0001 -> 0002" in result.stderr
    assert test_database_url not in result.stderr


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


# --- payload / payload_error (migration 0002) -----------------------------------------


def test_payload_may_be_null_when_error_is_recorded(migrated_db):
    store(migrated_db, make_event(payload=None, payload_error="contains U+0000"))
    with Session(migrated_db) as session:
        event = session.scalars(select(WebhookEvent)).one()
    assert event.payload is None
    assert event.payload_error == "contains U+0000"


@pytest.mark.parametrize(
    "payload, payload_error",
    [(None, None), ({"id": "evt_test_1"}, "both set")],
)
def test_exactly_one_of_payload_and_error(migrated_db, payload, payload_error):
    with pytest.raises(IntegrityError, match="payload_xor_error"):
        store(migrated_db, make_event(payload=payload, payload_error=payload_error))


def test_downgrade_refuses_to_drop_events_without_payload(migrated_db, alembic_config):
    store(migrated_db, make_event(payload=None, payload_error="contains U+0000"))
    with pytest.raises(Exception, match="cannot downgrade"):
        command.downgrade(alembic_config, "0001")
    # Clean up so the fixture's own downgrade can run.
    with migrated_db.begin() as connection:
        connection.exec_driver_sql("DELETE FROM webhook_events")
