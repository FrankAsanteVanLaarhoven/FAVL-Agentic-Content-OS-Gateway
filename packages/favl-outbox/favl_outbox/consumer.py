"""Consumer-side event parsing.

The stream still holds events published before the envelope existed. Those
are evidence of earlier milestones and useful replay material, so they are
not purged — consumers parse them instead.

A missing `schema_version` is NOT treated as version 1. It is identified as
version 0 (legacy) so the distinction survives into metrics and into any
downstream decision that depends on which fields are trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Counter

LEGACY_SCHEMA_VERSION = 0
CURRENT_SCHEMA_VERSION = 1

SCHEMA_PROCESSED = Counter(
    "favl_event_schema_processed_total",
    "Events parsed, by envelope schema version.",
    ["schema_version"],
)

SCHEMA_REJECTED = Counter(
    "favl_event_schema_rejected_total",
    "Events that could not be parsed.",
    ["reason"],
)


class EventParseError(ValueError):
    """Base for every rejection this module raises.

    A consumer catches this one type. Previously a malformed v1 envelope
    raised a bare KeyError, which escaped `except UnsupportedSchemaVersion`,
    killed the message handler, and left favl_event_schema_rejected_total —
    the counter whose whole purpose is to make dropped events visible —
    untouched.
    """


class UnsupportedSchemaVersion(EventParseError):
    def __init__(self, version: Any) -> None:
        super().__init__(f"unsupported event schema_version: {version!r}")
        self.version = version


class MalformedEnvelope(EventParseError):
    def __init__(self, missing: str) -> None:
        super().__init__(f"v1 envelope is missing required field: {missing}")
        self.missing = missing


@dataclass(frozen=True)
class DomainEvent:
    """Normalised internal form. Legacy and v1 both reduce to this."""

    event_id: str | None
    event_type: str
    aggregate_type: str | None
    aggregate_id: str | None
    aggregate_version: int | None
    occurred_at: datetime | None
    schema_version: int
    data: dict[str, Any]

    @property
    def is_legacy(self) -> bool:
        return self.schema_version == LEGACY_SCHEMA_VERSION

    @property
    def is_deduplicable(self) -> bool:
        """Legacy events carry no event_id, so they cannot be deduplicated."""
        return self.event_id is not None

    @property
    def is_orderable(self) -> bool:
        return self.aggregate_version is not None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_legacy_event(
    payload: dict[str, Any], subject: str | None = None
) -> DomainEvent:
    """Pre-envelope format: a bare domain body, no identifiers.

    Nothing is invented. Fields the old format never carried stay None so a
    consumer cannot mistake a guess for a real value.
    """
    SCHEMA_PROCESSED.labels("legacy").inc()
    return DomainEvent(
        event_id=None,
        event_type=subject or "unknown",
        aggregate_type=None,
        aggregate_id=payload.get("agent_id")
        or payload.get("connector_id")
        or payload.get("id"),
        aggregate_version=None,
        occurred_at=_parse_timestamp(payload.get("created_at")),
        schema_version=LEGACY_SCHEMA_VERSION,
        data=payload,
    )


def parse_v1_event(payload: dict[str, Any]) -> DomainEvent:
    for required in ("event_id", "event_type"):
        if not payload.get(required):
            SCHEMA_REJECTED.labels("malformed_envelope").inc()
            raise MalformedEnvelope(required)
    SCHEMA_PROCESSED.labels("1").inc()
    return DomainEvent(
        event_id=payload["event_id"],
        event_type=payload["event_type"],
        aggregate_type=payload.get("aggregate_type"),
        aggregate_id=payload.get("aggregate_id"),
        aggregate_version=payload.get("aggregate_version"),
        occurred_at=_parse_timestamp(payload.get("occurred_at")),
        schema_version=CURRENT_SCHEMA_VERSION,
        data=payload.get("data", {}),
    )


def parse_event(payload: dict[str, Any], subject: str | None = None) -> DomainEvent:
    schema_version = payload.get("schema_version")

    if schema_version is None:
        return parse_legacy_event(payload, subject)

    if schema_version == CURRENT_SCHEMA_VERSION:
        return parse_v1_event(payload)

    SCHEMA_REJECTED.labels("unsupported_version").inc()
    raise UnsupportedSchemaVersion(schema_version)


# --------------------------------------------------------------------- #
# aggregate ordering
# --------------------------------------------------------------------- #

APPLY = "apply"
IGNORE = "ignore"
QUARANTINE = "quarantine"


def version_decision(incoming: int | None, last_applied: int | None) -> str:
    """Monotonicity rule for a consumer projecting an aggregate.

    A higher version is not automatically acceptable: a gap means a missed
    event or an incomplete replay, and applying it would silently skip state.
    """
    if incoming is None:
        # Legacy events cannot be ordered; the consumer must decide.
        return QUARANTINE
    if last_applied is None:
        return APPLY if incoming == 1 else QUARANTINE
    if incoming <= last_applied:
        return IGNORE
    if incoming == last_applied + 1:
        return APPLY
    return QUARANTINE
