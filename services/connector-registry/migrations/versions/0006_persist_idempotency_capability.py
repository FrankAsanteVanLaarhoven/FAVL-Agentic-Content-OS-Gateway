"""persist adapter idempotency capability on the connector

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These were computed at create time and returned on the POST response,
    # then silently defaulted to false/"unsupported" on every subsequent GET.
    # A caller reading a connector back saw a weaker guarantee than the one
    # it was created with. Persisting them makes the read and write views
    # agree.
    op.add_column(
        "connectors",
        sa.Column(
            "supports_idempotency",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "connectors",
        sa.Column(
            "idempotency_mode",
            sa.String(length=32),
            nullable=False,
            server_default="unsupported",
        ),
    )


def downgrade() -> None:
    op.drop_column("connectors", "idempotency_mode")
    op.drop_column("connectors", "supports_idempotency")
