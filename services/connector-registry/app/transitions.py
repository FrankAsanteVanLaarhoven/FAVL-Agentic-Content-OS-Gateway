"""Applying a lifecycle transition.

One function, used by every transition endpoint, so the nine requirements a
transition must satisfy cannot be met by some endpoints and forgotten by
others:

    current-state precondition · authenticated actor · tenant ownership ·
    policy decision · aggregate-version increment · transactional outbox
    event · immutable audit record · idempotency · reason code

The row lock, the version increment, the outbox row and the audit row all
commit together. A transition that emitted its event but failed to record
the audit entry — or vice versa — would leave the two accounts of what
happened disagreeing, which is precisely the situation the audit trail exists
to rule out.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from favl_outbox import enqueue

from .lifecycle import (
    ConnectorState,
    Transition,
    TransitionError,
    find,
    idempotent_repeat,
    permitted_targets,
)
from .models import ConnectorAuditRecord, ConnectorRecord
from .outbox import OutboxEvent

logger = logging.getLogger(__name__)

TRANSITIONS_APPLIED = Counter(
    "favl_connector_transitions_total",
    "Connector lifecycle transitions, by event and outcome.",
    ["event", "outcome"],
)


class TransitionRejected(Exception):
    """The transition is not permitted from the connector's current state."""

    def __init__(self, message: str, current: str, permitted: list[str]) -> None:
        super().__init__(message)
        self.current = current
        self.permitted = permitted


class ReasonRequired(Exception):
    """A transition whose motive is operationally significant, with no reason."""


@dataclass(frozen=True)
class TransitionResult:
    connector: ConnectorRecord
    event: str
    repeated: bool
    """True when the connector already held the target state and the
    transition is idempotent, so no new version or event was produced."""


# State changes that stamp a timestamp column, so the common operational
# question — "when was this revoked?" — is answerable without replaying audit.
TIMESTAMP_COLUMN: dict[ConnectorState, str] = {
    ConnectorState.REVOKED: "revoked_at",
    ConnectorState.ARCHIVED: "archived_at",
    ConnectorState.DELETION_REQUESTED: "deletion_requested_at",
}


async def apply(
    session: AsyncSession,
    *,
    connector_id: uuid.UUID,
    target: ConnectorState,
    event: str | None,
    actor_id: str,
    tenant_id: str,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> TransitionResult:
    """Apply one transition, or explain why it is not permitted.

    Raises TransitionRejected (409), ReasonRequired (422), or LookupError
    (404 — including a tenant mismatch, since confirming an id exists in
    another tenant is itself a disclosure).
    """
    record = await session.get(ConnectorRecord, connector_id, with_for_update=True)
    if record is None or record.tenant_id != tenant_id:
        raise LookupError("Connector not found")

    current = record.status

    # A real edge wins over the idempotent-repeat shortcut, and is looked for
    # first. Self-loops that carry meaning — `credentials_rotated` on an
    # already-disabled connector, `configured` on an already-configured one —
    # are genuine transitions that must be recorded. Consulting the shortcut
    # first would swallow them as "you are already in that state" and lose the
    # audit entry, which for a credential rotation is the entire point.
    try:
        transition = find(current, target.value, event)
    except TransitionError as exc:
        repeat = idempotent_repeat(current, target.value)
        if repeat is not None:
            # Already in the target state, and getting there twice is a
            # success: an operator retrying after a timeout cannot know
            # whether the first attempt landed. No new version, no second
            # event — a duplicate would corrupt the version sequence.
            TRANSITIONS_APPLIED.labels(repeat.event, "repeated").inc()
            return TransitionResult(record, repeat.event, repeated=True)
        TRANSITIONS_APPLIED.labels(event or target.value, "rejected").inc()
        raise TransitionRejected(
            exc.reason, current, permitted_targets(current)
        ) from None

    if transition.requires_reason and not (reason or "").strip():
        TRANSITIONS_APPLIED.labels(transition.event, "no_reason").inc()
        raise ReasonRequired(
            f"{transition.event} requires a reason; an audit record without "
            "one cannot answer the only question later asked of it"
        )

    previous = current
    record.status = target.value
    record.version += 1
    record.state_reason = reason
    if (column := TIMESTAMP_COLUMN.get(target)) is not None:
        setattr(record, column, datetime.now(UTC))
    if transition.event == "connector.credentials_rotated":
        record.credentials_rotated_at = datetime.now(UTC)

    # Audit and event are staged on the same session as the row change, so
    # all three commit or none do.
    session.add(
        ConnectorAuditRecord(
            id=uuid.uuid4(),
            connector_id=record.id,
            tenant_id=tenant_id,
            from_state=previous,
            to_state=record.status,
            event=transition.event,
            actor_id=actor_id,
            reason=reason,
            aggregate_version=record.version,
            idempotency_key=idempotency_key,
        )
    )
    enqueue(
        session,
        OutboxEvent,
        subject=transition.event,
        payload={
            "connector_id": str(record.id),
            "name": record.name,
            "tenant_id": tenant_id,
            "from_state": previous,
            "to_state": record.status,
            "actor_id": actor_id,
            "reason": reason,
        },
        aggregate_type="connector",
        aggregate_id=str(record.id),
        aggregate_version=record.version,
    )
    await session.commit()

    TRANSITIONS_APPLIED.labels(transition.event, "applied").inc()
    logger.info(
        "connector.transition id=%s %s->%s event=%s actor=%s version=%d",
        record.id,
        previous,
        record.status,
        transition.event,
        actor_id,
        record.version,
    )
    return TransitionResult(record, transition.event, repeated=False)


async def audit_trail(
    session: AsyncSession, connector_id: uuid.UUID, tenant_id: str
) -> list[ConnectorAuditRecord]:
    """Every transition this connector has undergone, oldest first."""
    result = await session.execute(
        select(ConnectorAuditRecord)
        .where(
            ConnectorAuditRecord.connector_id == connector_id,
            ConnectorAuditRecord.tenant_id == tenant_id,
        )
        .order_by(ConnectorAuditRecord.aggregate_version)
    )
    return list(result.scalars())


__all__ = [
    "ReasonRequired",
    "Transition",
    "TransitionRejected",
    "TransitionResult",
    "apply",
    "audit_trail",
]
