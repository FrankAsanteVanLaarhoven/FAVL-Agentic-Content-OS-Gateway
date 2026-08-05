"""connector lifecycle states and immutable transition audit

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-05

DOWNGRADE IS LOSSY, and deliberately so. Reversing this migration drops
`connector_audit` — every recorded transition, with its actor and reason —
because the table did not exist at 0007 and there is nowhere to put it. It
also parks any connector sitting in one of the five new states into
`disabled`, since the narrower constraint would otherwise reject data this
migration's own upgrade created.

The rollback is verified to run (see gates/G3/), not recommended. Before
downgrading a deployment that has served traffic, dump `connector_audit`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = (
    "draft",
    "installed",
    "configured",
    "validated",
    "enabled",
    "disabled",
    "revoked",
    "deletion_requested",
    "archived",
    "deleted",
)

# Built explicitly rather than from a tuple repr: `str(("draft",))` is
# `('draft',)`, whose trailing comma is a syntax error in SQL. A constraint
# that is correct only because the list happens to have more than one entry
# is a trap for whoever shortens it.
STATE_LIST = ", ".join(f"'{state}'" for state in STATES)


def upgrade() -> None:
    # The check constraint is widened BEFORE any row can use a new state.
    # Doing it the other way round would let a transition write a value the
    # constraint rejects, which surfaces as a 500 rather than a 409.
    op.drop_constraint("ck_connectors_status", "connectors", type_="check")
    op.create_check_constraint(
        "ck_connectors_status",
        "connectors",
        f"status IN ({STATE_LIST})",
    )

    op.add_column(
        "connectors",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connectors",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "connectors",
        sa.Column("credentials_rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("connectors", sa.Column("state_reason", sa.Text(), nullable=True))

    # Immutable audit. Separate from outbox_events on purpose: an outbox row
    # is deleted or archived once delivered, and its retention is a delivery
    # concern. An audit record answers "who changed this, when, from what, to
    # what, and why" long after the event has left the stream.
    op.create_table(
        "connector_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=False),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # RESTRICT, not CASCADE: the audit trail must outlive any attempt to
        # remove the connector it describes. That is the entire point of it.
        sa.ForeignKeyConstraint(
            ["connector_id"], ["connectors.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_connector_audit_connector",
        "connector_audit",
        ["connector_id", "aggregate_version"],
    )
    op.create_index(
        "ix_connector_audit_tenant", "connector_audit", ["tenant_id", "recorded_at"]
    )
    # One row per aggregate version: a transition that produced two audit
    # entries, or none, is a bug the database refuses rather than hides.
    op.create_unique_constraint(
        "uq_connector_audit_version",
        "connector_audit",
        ["connector_id", "aggregate_version"],
    )

    # Append-only at the database level, so a later code change cannot quietly
    # rewrite history. Revoking UPDATE and DELETE is the enforcement; the
    # comment is the explanation.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION connector_audit_is_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'connector_audit is append-only: % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER connector_audit_immutable
        BEFORE UPDATE OR DELETE ON connector_audit
        FOR EACH ROW EXECUTE FUNCTION connector_audit_is_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS connector_audit_immutable ON connector_audit")
    op.execute("DROP FUNCTION IF EXISTS connector_audit_is_append_only()")
    op.drop_constraint("uq_connector_audit_version", "connector_audit", type_="unique")
    op.drop_index("ix_connector_audit_tenant", table_name="connector_audit")
    op.drop_index("ix_connector_audit_connector", table_name="connector_audit")
    op.drop_table("connector_audit")

    op.drop_column("connectors", "state_reason")
    op.drop_column("connectors", "credentials_rotated_at")
    op.drop_column("connectors", "archived_at")
    op.drop_column("connectors", "revoked_at")

    # Any row in a state the old constraint forbids must be parked somewhere
    # legal before the narrower constraint is restored, or the migration
    # fails on data it created itself.
    op.execute(
        "UPDATE connectors SET status = 'disabled' "
        "WHERE status IN ('installed', 'configured', 'validated', "
        "'revoked', 'archived')"
    )
    op.drop_constraint("ck_connectors_status", "connectors", type_="check")
    op.create_check_constraint(
        "ck_connectors_status",
        "connectors",
        "status IN ('draft', 'enabled', 'disabled', 'deletion_requested', 'deleted')",
    )
