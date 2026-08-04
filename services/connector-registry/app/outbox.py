"""Outbox wiring for the connector registry.

Its own table in its own database. Nothing is shared with the orchestrator
except the library code.
"""

from __future__ import annotations

import os

from favl_outbox import (
    JetStreamConnection,
    OutboxPublisher,
    enforce_duplicate_window_invariant,
    make_outbox_model,
    operational_delays_from_env,
    retry_policy_from_env,
)

from .db import Base, SessionLocal

SERVICE = "connector-registry"

OutboxEvent = make_outbox_model(Base)

connection = JetStreamConnection()

# Release-blocking: raises DuplicateWindowTooSmall if the deployed retry
# configuration could let a retry escape the stream's duplicate window.
# Runs at import so the service fails to start rather than silently
# delivering an event twice under an unlucky retry sequence.
WINDOW_UTILISATION = enforce_duplicate_window_invariant(SERVICE)

publisher = OutboxPublisher(
    service=SERVICE,
    session_factory=SessionLocal,
    model=OutboxEvent,
    connection=connection,
    retry_policy=retry_policy_from_env(),
    delays=operational_delays_from_env(),
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
