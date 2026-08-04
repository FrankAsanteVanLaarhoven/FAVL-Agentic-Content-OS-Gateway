"""connector lifecycle status and invocation records

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "connectors",
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="enabled"
        ),
    )
    op.add_column(
        "connectors",
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connectors", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "connectors", sa.Column("deleted_by", sa.String(length=255), nullable=True)
    )

    # Carry the old boolean across before dropping it: a connector that was
    # disabled must not silently become executable.
    op.execute(
        "UPDATE connectors SET status = CASE WHEN enabled THEN 'enabled' "
        "ELSE 'disabled' END"
    )
    op.drop_column("connectors", "enabled")

    op.create_check_constraint(
        "ck_connectors_status",
        "connectors",
        "status IN ('draft', 'enabled', 'disabled', 'deletion_requested', 'deleted')",
    )

    op.create_table(
        "invocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_version", sa.Integer(), nullable=False),
        sa.Column("adapter_kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("aggregate_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column(
            "audit_metadata", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # RESTRICT, not CASCADE: an invocation is an audit record and must
        # outlive any attempt to remove the connector it referenced.
        sa.ForeignKeyConstraint(
            ["connector_id"], ["connectors.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('accepted', 'running', 'succeeded', 'failed_retryable', "
            "'failed_terminal', 'timed_out', 'cancelled')",
            name="ck_invocations_status",
        ),
    )
    # The idempotency contract, enforced by the database rather than by a
    # read-then-write race in application code.
    op.create_unique_constraint(
        "uq_invocations_idempotency",
        "invocations",
        ["tenant_id", "connector_id", "idempotency_key"],
    )
    op.create_index("ix_invocations_connector", "invocations", ["connector_id", "created_at"])
    op.create_index("ix_invocations_status", "invocations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_invocations_status", table_name="invocations")
    op.drop_index("ix_invocations_connector", table_name="invocations")
    op.drop_constraint("uq_invocations_idempotency", "invocations", type_="unique")
    op.drop_table("invocations")

    op.add_column(
        "connectors",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE connectors SET enabled = (status = 'enabled')")
    op.drop_constraint("ck_connectors_status", "connectors", type_="check")
    op.drop_column("connectors", "deleted_by")
    op.drop_column("connectors", "deleted_at")
    op.drop_column("connectors", "deletion_requested_at")
    op.drop_column("connectors", "status")
    op.drop_column("connectors", "config")
