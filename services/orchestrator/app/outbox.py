"""Outbox wiring for the orchestrator."""

from __future__ import annotations

import os

from favl_outbox import JetStreamConnection, OutboxPublisher, make_outbox_model

from .db import Base, SessionLocal

SERVICE = "orchestrator"

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
    """Test hook.

    Set OUTBOX_PUBLISHER_ENABLED=false to accept writes without draining
    them, which reproduces "committed but not yet published" deterministically
    instead of relying on a race.
    """
    return os.getenv("OUTBOX_PUBLISHER_ENABLED", "true").lower() not in {
        "false",
        "0",
        "no",
    }
