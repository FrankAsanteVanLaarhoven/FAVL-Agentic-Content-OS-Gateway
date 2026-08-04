"""Outbox table definition.

The table is created per service database via `make_outbox_model(Base)`, so
each service owns its own outbox rows and its own migration history. Nothing
is shared at the data layer — only this code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

TABLE_NAME = "outbox_events"

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_DEAD = "dead"
ALL_STATUSES = (STATUS_PENDING, STATUS_PUBLISHED, STATUS_DEAD)

DEFAULT_MAX_ATTEMPTS = 8


def _utcnow() -> datetime:
    return datetime.now(UTC)


def make_outbox_model(base: Any) -> Any:
    """Bind the outbox table to a service's declarative Base."""

    class OutboxEvent(base):  # type: ignore[misc]
        __tablename__ = TABLE_NAME
        __table_args__ = (
            CheckConstraint(
                "status IN ('pending', 'published', 'dead')",
                name="ck_outbox_events_status",
            ),
            # Partial index: the publisher only ever scans pending rows, and
            # published rows dominate the table over time.
            #
            # The predicate MUST be textually identical to migration 0002's.
            # A previous version used `func.lower("status")`, which SQLAlchemy
            # coerces to a bound literal rather than a column reference: it
            # compiled to `WHERE lower('status') = 'pending'`, a constant
            # false, producing a permanently empty index that Postgres would
            # never match to the publisher's plain-equality claim query.
            Index(
                "ix_outbox_events_due",
                "next_attempt_at",
                postgresql_where=text("status = 'pending'"),
            ),
            # Covers the claim query's ORDER BY created_at within the pending
            # set, so draining a backlog is an index scan rather than a sort.
            Index(
                "ix_outbox_events_claim",
                "created_at",
                postgresql_where=text("status = 'pending'"),
            ),
            Index("ix_outbox_events_status", "status"),
        )

        # Doubles as the JetStream Nats-Msg-Id, so a republish after a crash
        # is collapsed by the stream's duplicate window.
        id: Mapped[uuid.UUID] = mapped_column(
            UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
        )
        aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
        aggregate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
        # Monotonic per aggregate. Consumers use it to reject stale events and
        # to detect gaps without trusting stream order.
        aggregate_version: Mapped[int] = mapped_column(
            Integer, nullable=False, default=1, server_default="1"
        )
        subject: Mapped[str] = mapped_column(String(255), nullable=False)
        schema_version: Mapped[int] = mapped_column(
            Integer, nullable=False, default=1, server_default="1"
        )
        # The domain body only. The wire envelope is built at publish time so
        # the row stays canonical and the envelope can be versioned.
        payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

        status: Mapped[str] = mapped_column(
            String(16),
            nullable=False,
            default=STATUS_PENDING,
            server_default=STATUS_PENDING,
        )
        attempts: Mapped[int] = mapped_column(
            Integer, nullable=False, default=0, server_default="0"
        )
        max_attempts: Mapped[int] = mapped_column(
            Integer,
            nullable=False,
            default=DEFAULT_MAX_ATTEMPTS,
            server_default=str(DEFAULT_MAX_ATTEMPTS),
        )
        next_attempt_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            nullable=False,
            default=_utcnow,
            server_default=func.now(),
        )
        last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

        created_at: Mapped[datetime] = mapped_column(
            DateTime(timezone=True),
            nullable=False,
            default=_utcnow,
            server_default=func.now(),
        )
        published_at: Mapped[datetime | None] = mapped_column(
            DateTime(timezone=True), nullable=True
        )
        stream_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

        def __repr__(self) -> str:  # pragma: no cover - debugging aid
            return (
                f"<OutboxEvent {self.id} {self.subject} "
                f"status={self.status} attempts={self.attempts}>"
            )

    return OutboxEvent
