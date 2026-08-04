"""Outbox wiring for the connector registry.

Its own table in its own database. Nothing is shared with the orchestrator
except the library code.
"""

from __future__ import annotations

import os

from favl_outbox import JetStreamConnection, OutboxPublisher, make_outbox_model

from .db import Base, SessionLocal

SERVICE = "connector-registry"

OutboxEvent = make_outbox_model(Base)

connection = JetStreamConnection()

publisher = OutboxPublisher(
    service=SERVICE,
    session_factory=SessionLocal,
    model=OutboxEvent,
    connection=connection,
    batch_size=int(os.getenv("OUTBOX_BATCH_SIZE", "100")),
    poll_interval=float(os.getenv("OUTBOX_POLL_INTERVAL", "0.25")),
    idle_interval=float(os.getenv("OUTBOX_IDLE_INTERVAL", "1.0")),
)


def publisher_enabled() -> bool:
    """See services/orchestrator/app/outbox.py for the test-hook rationale."""
    return os.getenv("OUTBOX_PUBLISHER_ENABLED", "true").lower() not in {
        "false",
        "0",
        "no",
    }
