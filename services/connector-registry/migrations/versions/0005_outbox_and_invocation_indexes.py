"""add claim-supporting outbox index

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
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


    # The console lists invocations newest-first with an optional status or
    # connector filter. ix_invocations_connector leads with connector_id, so
    # an unfiltered listing could not use it and sorted the whole table.
    op.create_index(
        "ix_invocations_created_desc",
        "invocations",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_invocations_status_created",
        "invocations",
        ["status", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_invocations_status_created", table_name="invocations")
    op.drop_index("ix_invocations_created_desc", table_name="invocations")
    op.drop_index("ix_outbox_events_claim", table_name="outbox_events")
