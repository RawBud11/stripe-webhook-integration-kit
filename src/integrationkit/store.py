"""Persistence of verified webhook events."""

from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert

from integrationkit.events import ParsedEvent
from integrationkit.models import WebhookEvent


def store_event(engine: Engine, event: ParsedEvent, raw_body: bytes, signature_ts: int) -> bool:
    """Insert the event once. Returns False if this event id was already stored."""
    statement = (
        insert(WebhookEvent)
        .values(
            event_id=event.event_id,
            event_type=event.event_type,
            session_id=event.session_id,
            livemode=event.livemode,
            raw_body=raw_body,
            payload=event.payload,
            payload_error=event.payload_error,
            signature_ts=datetime.fromtimestamp(signature_ts, UTC),
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
        .returning(WebhookEvent.id)
    )
    with engine.begin() as connection:
        return connection.execute(statement).first() is not None
