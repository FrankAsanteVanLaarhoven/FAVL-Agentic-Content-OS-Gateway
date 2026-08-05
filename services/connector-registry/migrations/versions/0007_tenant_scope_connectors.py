"""scope connectors to a tenant

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Invocations became tenant-scoped before connectors did, which left an
    # inconsistent model: tenant A could invoke tenant B's connector but not
    # see the resulting invocation. A half-applied isolation boundary is
    # worse than none, because it reads as protection.
    op.add_column(
        "connectors",
        sa.Column(
            "tenant_id",
            sa.String(length=255),
            nullable=False,
            server_default="default",
        ),
    )
    # Names are unique per tenant, not globally: two tenants must be able to
    # register a connector called "github" without colliding, and a global
    # constraint would leak the existence of another tenant's connector.
    op.drop_constraint("uq_connectors_name", "connectors", type_="unique")
    op.create_unique_constraint(
        "uq_connectors_tenant_name", "connectors", ["tenant_id", "name"]
    )
    op.create_index("ix_connectors_tenant", "connectors", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_connectors_tenant", table_name="connectors")
    op.drop_constraint("uq_connectors_tenant_name", "connectors", type_="unique")
    op.create_unique_constraint("uq_connectors_name", "connectors", ["name"])
    op.drop_column("connectors", "tenant_id")
