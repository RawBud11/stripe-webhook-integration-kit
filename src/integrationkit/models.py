"""SQLAlchemy models. The Alembic migrations in migrations/ must match these."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    LargeBinary,
    MetaData,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint and index names, so migrations do not depend on database defaults.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class WebhookEvent(Base):
    """One verified webhook event, stored once per Stripe event id."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        # Exactly one of the two: a stored payload, or the reason it could not be stored.
        CheckConstraint("(payload IS NULL) <> (payload_error IS NULL)", name="payload_xor_error"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True)
    event_type: Mapped[str] = mapped_column(Text)
    # data.object.id; indexed but not unique, because Stripe can send two distinct
    # events for the same object.
    session_id: Mapped[str | None] = mapped_column(Text, index=True)
    livemode: Mapped[bool]
    # The exact bytes that were verified. Never re-serialised.
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    # Parsed copy for querying. JSONB drops whitespace, key order and duplicate keys.
    # NULL when JSONB cannot hold the payload (e.g. \u0000); payload_error says why.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    payload_error: Mapped[str | None] = mapped_column(Text)
    signature_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
