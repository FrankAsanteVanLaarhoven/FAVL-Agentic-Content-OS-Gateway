"""create connectors table

Revision ID: 0001
Revises:
Create Date: 2026-08-04
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('http', 'mcp', 'webhook', 'internal')",
            name="ck_connectors_kind",
        ),
    )
    op.create_unique_constraint("uq_connectors_name", "connectors", ["name"])
    op.create_index("ix_connectors_created_at", "connectors", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_connectors_created_at", table_name="connectors")
    op.drop_constraint("uq_connectors_name", "connectors", type_="unique")
    op.drop_table("connectors")
