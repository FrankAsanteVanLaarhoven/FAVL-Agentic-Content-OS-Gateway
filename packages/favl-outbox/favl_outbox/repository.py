"""Enqueue side of the outbox."""

from __future__ import annotations

import uuid
from typing import Any


def enqueue(
    session: Any,
    model: Any,
    *,
    subject: str,
    payload: dict[str, Any],
    aggregate_type: str,
    aggregate_id: str | None = None,
    max_attempts: int | None = None,
) -> Any:
    """Stage an event on the caller's session.

    Deliberately does not commit. The caller must commit the domain row and
    this event in the same transaction — that single commit is the entire
    point of the pattern.
    """
    event = model(
        id=uuid.uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        subject=subject,
        payload=payload,
    )
    if max_attempts is not None:
        event.max_attempts = max_attempts
    session.add(event)
    return event
