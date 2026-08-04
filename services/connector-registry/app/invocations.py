"""Invocation runtime: idempotency, deadlines, lifecycle, audit.

Every state change is committed with its event in the same transaction, so
the outbox guarantee from M1.2 extends to invocation lifecycle events.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from favl_outbox import enqueue
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .adapters.base import (
    ConnectorContext,
    ErrorCode,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from .adapters.registry import AdapterRegistry, UnknownAdapterKind
from .models import ConnectorRecord, ConnectorStatus, InvocationRecord
from .outbox import OutboxEvent

logger = logging.getLogger(__name__)

SERVICE = "connector-registry"

INVOCATIONS = Counter(
    "favl_connector_invocations_total",
    "Connector invocations by adapter kind and terminal status.",
    ["kind", "status"],
)
INVOCATION_ERRORS = Counter(
    "favl_connector_invocation_errors_total",
    "Connector invocation failures by classified error code.",
    ["kind", "error_code"],
)
INVOCATION_RETRIES = Counter(
    "favl_connector_invocation_retries_total",
    "Invocations replayed under an existing idempotency key.",
    ["kind", "outcome"],
)
INVOCATION_TIMEOUTS = Counter(
    "favl_connector_invocation_timeouts_total",
    "Invocations that exceeded their deadline.",
    ["kind"],
)
INVOCATION_DURATION = Histogram(
    "favl_connector_invocation_duration_seconds",
    "Wall-clock duration of a connector invocation.",
    ["kind", "status"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
INVOCATIONS_IN_FLIGHT = Gauge(
    "favl_connector_invocations_in_flight",
    "Invocations currently executing.",
    ["kind"],
)


class ConnectorNotExecutable(Exception):
    """Connector exists but its lifecycle state forbids execution."""

    def __init__(self, status: str, code: ErrorCode, http_status: int) -> None:
        super().__init__(f"connector is {status}")
        self.status = status
        self.code = code
        self.http_status = http_status


@dataclass
class ReplayedInvocation:
    """An existing invocation matched by idempotency key."""

    record: InvocationRecord
    in_flight: bool


def check_executable(record: ConnectorRecord) -> None:
    """Reject every non-executable lifecycle state explicitly.

    410 for deleted rather than 404: the caller is authenticated and
    authorised to know the connector existed, and Gone is actionable where a
    bare Not Found is not.
    """
    status = record.status
    if status == ConnectorStatus.ENABLED.value:
        return
    if status == ConnectorStatus.DELETION_REQUESTED.value:
        raise ConnectorNotExecutable(status, ErrorCode.CONNECTOR_DELETION_PENDING, 409)
    if status == ConnectorStatus.DELETED.value:
        raise ConnectorNotExecutable(status, ErrorCode.CONNECTOR_GONE, 410)
    # draft and disabled
    raise ConnectorNotExecutable(status, ErrorCode.CONNECTOR_DISABLED, 409)


def _emit(
    session: AsyncSession,
    record: InvocationRecord,
    event_type: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Stage a lifecycle event. Never carries secrets or provider bodies."""
    payload: dict[str, Any] = {
        "invocation_id": str(record.id),
        "connector_id": str(record.connector_id),
        "connector_version": record.connector_version,
        "adapter_kind": record.adapter_kind,
        "tenant_id": record.tenant_id,
        "actor_id": record.actor_id,
        "operation": record.operation,
        "status": record.status,
        "attempt": record.attempt,
    }
    if record.trace_id:
        payload["trace_id"] = record.trace_id
    if record.error_code:
        payload["error_code"] = record.error_code
        payload["retryable"] = record.retryable
    if record.started_at and record.completed_at:
        payload["duration_ms"] = round(
            (record.completed_at - record.started_at).total_seconds() * 1000, 3
        )
    if extra:
        payload.update(extra)

    enqueue(
        session,
        OutboxEvent,
        subject=event_type,
        payload=payload,
        aggregate_type="invocation",
        aggregate_id=str(record.id),
        aggregate_version=record.aggregate_version,
    )


async def find_existing(
    session: AsyncSession, tenant_id: str, connector_id: uuid.UUID, key: str
) -> InvocationRecord | None:
    result = await session.execute(
        select(InvocationRecord).where(
            InvocationRecord.tenant_id == tenant_id,
            InvocationRecord.connector_id == connector_id,
            InvocationRecord.idempotency_key == key,
        )
    )
    return result.scalar_one_or_none()


async def accept(
    session: AsyncSession,
    connector: ConnectorRecord,
    *,
    operation: str,
    idempotency_key: str,
    actor_id: str,
    tenant_id: str,
    timeout_seconds: float,
    trace_id: str | None,
) -> tuple[InvocationRecord, ReplayedInvocation | None]:
    """Create the invocation, or return the existing one for this key.

    The accepted row and its event commit together, so an invocation that
    exists always has an accepted event, and vice versa.
    """
    existing = await find_existing(session, tenant_id, connector.id, idempotency_key)
    if existing is not None:
        in_flight = existing.status in (
            InvocationStatus.ACCEPTED.value,
            InvocationStatus.RUNNING.value,
        )
        INVOCATION_RETRIES.labels(
            connector.kind, "in_flight" if in_flight else "replayed"
        ).inc()
        return existing, ReplayedInvocation(existing, in_flight)

    now = datetime.now(timezone.utc)
    record = InvocationRecord(
        id=uuid.uuid4(),
        connector_id=connector.id,
        connector_version=connector.version,
        adapter_kind=connector.kind,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        tenant_id=tenant_id,
        operation=operation,
        status=InvocationStatus.ACCEPTED.value,
        attempt=1,
        aggregate_version=1,
        deadline_at=now + timedelta(seconds=timeout_seconds),
        trace_id=trace_id,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent request with the same key won the race. Reuse its row.
        await session.rollback()
        existing = await find_existing(
            session, tenant_id, connector.id, idempotency_key
        )
        if existing is None:
            raise
        INVOCATION_RETRIES.labels(connector.kind, "race").inc()
        return existing, ReplayedInvocation(existing, True)

    _emit(session, record, "connector.invocation.accepted")
    await session.commit()
    return record, None


async def execute(
    session: AsyncSession,
    registry: AdapterRegistry,
    connector: ConnectorRecord,
    record: InvocationRecord,
    payload: dict[str, Any],
) -> InvocationRecord:
    """Run the adapter and persist the terminal state with its event."""
    try:
        adapter = registry.get(connector.kind)
    except UnknownAdapterKind as exc:
        return await _finalise(
            session,
            record,
            InvocationResult.failure(ErrorCode.UNKNOWN_KIND, str(exc)),
            connector.kind,
        )

    record.status = InvocationStatus.RUNNING.value
    record.started_at = datetime.now(timezone.utc)
    record.aggregate_version += 1
    _emit(session, record, "connector.invocation.started")
    await session.commit()

    context = ConnectorContext(
        connector_id=str(connector.id),
        connector_version=connector.version,
        kind=connector.kind,
        config=dict(connector.config or {}),
        tenant_id=record.tenant_id,
    )
    request = InvocationRequest(
        invocation_id=str(record.id),
        connector_id=str(connector.id),
        operation=record.operation,
        payload=payload,
        idempotency_key=record.idempotency_key,
        actor_id=record.actor_id,
        tenant_id=record.tenant_id,
        deadline_at=record.deadline_at,
        trace_id=record.trace_id,
        attempt=record.attempt,
    )

    budget = request.seconds_remaining(datetime.now(timezone.utc))
    INVOCATIONS_IN_FLIGHT.labels(connector.kind).inc()
    try:
        # The deadline is enforced here as well as inside the adapter, so an
        # adapter that ignores or mishandles its budget cannot hang the
        # invocation indefinitely.
        result = await asyncio.wait_for(
            adapter.invoke(request, context), timeout=budget
        )
    except (TimeoutError, asyncio.TimeoutError):
        INVOCATION_TIMEOUTS.labels(connector.kind).inc()
        result = InvocationResult.failure(
            ErrorCode.UPSTREAM_TIMEOUT,
            f"adapter exceeded the {budget:.1f}s deadline",
            started_at=record.started_at,
            completed_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        logger.exception(
            "invocation.adapter_error invocation_id=%s kind=%s", record.id, connector.kind
        )
        result = InvocationResult.failure(
            ErrorCode.UPSTREAM_ERROR,
            f"{type(exc).__name__}: {exc}"[:500],
            started_at=record.started_at,
            completed_at=datetime.now(timezone.utc),
        )
    finally:
        INVOCATIONS_IN_FLIGHT.labels(connector.kind).dec()

    return await _finalise(session, record, result, connector.kind)


TERMINAL_EVENT = {
    InvocationStatus.SUCCEEDED: "connector.invocation.succeeded",
    InvocationStatus.TIMED_OUT: "connector.invocation.timed_out",
    InvocationStatus.FAILED_RETRYABLE: "connector.invocation.failed",
    InvocationStatus.FAILED_TERMINAL: "connector.invocation.failed",
    InvocationStatus.CANCELLED: "connector.invocation.failed",
}


async def _finalise(
    session: AsyncSession,
    record: InvocationRecord,
    result: InvocationResult,
    kind: str,
) -> InvocationRecord:
    now = datetime.now(timezone.utc)
    record.status = result.status.value
    record.completed_at = result.completed_at or now
    record.started_at = record.started_at or result.started_at or now
    record.provider_request_id = result.provider_request_id
    record.error_code = result.error_code.value if result.error_code else None
    record.error_detail = result.error_detail or None
    record.retryable = result.retryable
    record.output = result.output or None
    record.audit_metadata = result.audit_metadata or {}
    record.aggregate_version += 1

    duration = (record.completed_at - record.started_at).total_seconds()
    INVOCATIONS.labels(kind, record.status).inc()
    INVOCATION_DURATION.labels(kind, record.status).observe(max(duration, 0.0))
    if record.error_code:
        INVOCATION_ERRORS.labels(kind, record.error_code).inc()

    _emit(session, record, TERMINAL_EVENT[result.status])
    await session.commit()

    logger.info(
        "invocation.completed id=%s kind=%s status=%s error=%s duration_ms=%.1f",
        record.id,
        kind,
        record.status,
        record.error_code,
        duration * 1000,
    )
    return record
