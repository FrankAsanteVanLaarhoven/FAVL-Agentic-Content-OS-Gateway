"""scope agents to a tenant

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Connectors and invocations were scoped before agents were, which left
    # the isolation fully bypassable: tenant A created an agent referencing
    # tenant B's connector id, invoked it, and the orchestrator called the
    # registry's /internal surface — which performed no tenant check — with
    # the mesh service token. A tenant-scoped public path plus an unscoped
    # internal path is not isolation.
    op.add_column(
        "agents",
        sa.Column(
            "tenant_id",
            sa.String(length=255),
            nullable=False,
            server_default="default",
        ),
    )
    op.drop_constraint("uq_agents_name", "agents", type_="unique")
    op.create_unique_constraint("uq_agents_tenant_name", "agents", ["tenant_id", "name"])
    op.create_index("ix_agents_tenant", "agents", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_agents_tenant", table_name="agents")
    op.drop_constraint("uq_agents_tenant_name", "agents", type_="unique")
    op.create_unique_constraint("uq_agents_name", "agents", ["name"])
    op.drop_column("agents", "tenant_id")
