"""payload nullable, add payload_error

A validly signed event whose JSON PostgreSQL's jsonb cannot hold (for example a
\\u0000 escape) is still stored: raw_body as received, payload NULL, payload_error set.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("payload_error", sa.Text(), nullable=True))
    op.alter_column("webhook_events", "payload", existing_type=postgresql.JSONB(), nullable=True)
    op.create_check_constraint(
        op.f("ck_webhook_events_payload_xor_error"),
        "webhook_events",
        "(payload IS NULL) <> (payload_error IS NULL)",
    )


def downgrade() -> None:
    # Rows without a payload cannot satisfy the old NOT NULL column; refuse rather than
    # silently delete stored events.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM webhook_events WHERE payload IS NULL) THEN
                RAISE EXCEPTION 'cannot downgrade: events with payload_error exist';
            END IF;
        END $$;
        """
    )
    op.drop_constraint(op.f("ck_webhook_events_payload_xor_error"), "webhook_events", type_="check")
    op.alter_column("webhook_events", "payload", existing_type=postgresql.JSONB(), nullable=False)
    op.drop_column("webhook_events", "payload_error")
