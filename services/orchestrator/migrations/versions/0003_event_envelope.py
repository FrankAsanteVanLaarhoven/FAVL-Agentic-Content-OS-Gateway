"""add event envelope fields and aggregate versioning

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("aggregate_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "outbox_events",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "agents",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    # Consumers deduplicate on event_id and order on aggregate_version, so
    # both are looked up together when reconciling an aggregate.
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "aggregate_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_column("agents", "version")
    op.drop_column("outbox_events", "schema_version")
    op.drop_column("outbox_events", "aggregate_version")
