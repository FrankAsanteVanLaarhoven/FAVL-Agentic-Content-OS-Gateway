"""The published event envelope.

Broker deduplication is a safety net with an expiry, not a contract. A
consumer can still see the same event twice when the duplicate window lapses,
a stream is replayed or restored, an ack is lost, a subject is mirrored, or
an operator republishes. Consumers must therefore deduplicate themselves, and
they need stable identifiers to do it with.

`event_id` is the outbox row id: stable across every republication of that
event, and the same value used as `Nats-Msg-Id`. Deduplicate on it.

`aggregate_version` is a monotonic counter per aggregate. Use it to discard
stale events and to detect gaps; do not rely on stream order for either.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1


def build_envelope(
    *,
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str | None,
    aggregate_version: int,
    occurred_at: datetime,
    data: dict[str, Any],
    schema_version: int = SCHEMA_VERSION,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "occurred_at": occurred_at.isoformat(),
        "schema_version": schema_version,
        "data": data,
    }


def envelope_from_row(row: Any) -> dict[str, Any]:
    """Build the wire envelope from a persisted outbox row."""
    return build_envelope(
        event_id=str(row.id),
        event_type=row.subject,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        aggregate_version=row.aggregate_version,
        occurred_at=row.created_at,
        data=row.payload,
        schema_version=row.schema_version,
    )
