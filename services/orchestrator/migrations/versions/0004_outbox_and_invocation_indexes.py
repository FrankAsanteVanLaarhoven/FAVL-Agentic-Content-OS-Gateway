"""add claim-supporting outbox index

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The publisher claims with:
    #   WHERE status = 'pending' AND next_attempt_at <= now()
    #   ORDER BY created_at LIMIT n FOR UPDATE SKIP LOCKED
    # ix_outbox_events_due covers the predicate but not the ordering, so
    # draining a backlog sorted every candidate row. This partial index makes
    # the ORDER BY an index scan and stays small because it only covers
    # pending rows.
    op.create_index(
        "ix_outbox_events_claim",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_claim", table_name="outbox_events")
