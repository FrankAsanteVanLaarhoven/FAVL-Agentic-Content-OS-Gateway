"""ORM models for the connector registry."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ConnectorStatus(str, Enum):
    """The single authoritative lifecycle field.

    A separate `enabled` boolean was removed rather than kept alongside this:
    two sources of truth for whether a connector may run will eventually
    disagree, and the disagreement surfaces as a security bug.
    """

    DRAFT = "draft"
    ENABLED = "enabled"
    DISABLED = "disabled"
    DELETION_REQUESTED = "deletion_requested"
    DELETED = "deleted"


# States in which a connector may accept traffic.
EXECUTABLE_STATUSES = frozenset({ConnectorStatus.ENABLED})


class ConnectorRecord(Base):
    __tablename__ = "connectors"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'enabled', 'disabled', "
            "'deletion_requested', 'deleted')",
            name="ck_connectors_status",
        ),
        CheckConstraint(
            "kind IN ('http', 'mcp', 'webhook', 'internal')",
            name="ck_connectors_kind",
        ),
        # Per tenant, not global: two tenants must both be able to register
        # "github", and a global constraint would leak the existence of
        # another tenant's connector through a 409.
        UniqueConstraint("tenant_id", "name", name="uq_connectors_tenant_name"),
        Index("ix_connectors_tenant", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default", server_default="default"
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    # Adapter-specific settings. Secrets appear only as references.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=ConnectorStatus.ENABLED.value,
        server_default=ConnectorStatus.ENABLED.value,
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Derived from the adapter at create/validate time and stored, so a read
    # reports the same guarantee the connector was created with.
    supports_idempotency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    idempotency_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unsupported",
        server_default="unsupported",
    )
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )

    @property
    def executable(self) -> bool:
        return self.status in {s.value for s in EXECUTABLE_STATUSES}


class InvocationRecord(Base):
    """Persisted invocation lifecycle.

    An invocation is not a request/response pair: it has a state machine that
    must survive the process, both for idempotent replay and for audit.
    """

    __tablename__ = "invocations"
    __table_args__ = (
        # The idempotency contract. Scoped by tenant so one tenant cannot
        # collide with, or observe, another's keys.
        UniqueConstraint(
            "tenant_id",
            "connector_id",
            "idempotency_key",
            name="uq_invocations_idempotency",
        ),
        CheckConstraint(
            "status IN ('accepted', 'running', 'succeeded', 'failed_retryable', "
            "'failed_terminal', 'timed_out', 'cancelled')",
            name="ck_invocations_status",
        ),
        Index("ix_invocations_connector", "connector_id", "created_at"),
        Index("ix_invocations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connector_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Pinned at invocation time so an audit row still reflects the connector
    # as it was configured then, not as it is now.
    connector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Stored so a repeated idempotency key returns the original result.
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
    )
