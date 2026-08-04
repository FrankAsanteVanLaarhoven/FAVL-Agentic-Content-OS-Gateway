"""ORM models for the connector registry."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConnectorRecord(Base):
    __tablename__ = "connectors"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Stored as text: secrets never live in connector records, only the
    # endpoint. Credentials resolve from the secret manager at invoke time.
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Monotonic per aggregate; stamped onto every emitted event so consumers
    # can reject stale deliveries without trusting stream order.
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
