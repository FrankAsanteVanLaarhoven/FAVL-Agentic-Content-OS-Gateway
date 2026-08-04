from __future__ import annotations

import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from favl_outbox import enqueue

from . import invocations as inv
from .adapters.base import ConnectorContext, InvocationStatus
from .adapters.registry import UnknownAdapterKind, build_registry, registry_snapshot
from .db import SessionLocal, engine, get_session
from .identity import CallerIdentity, current_identity, current_tenant
from .models import ConnectorRecord, ConnectorStatus, InvocationRecord
from .outbox import (
    WINDOW_UTILISATION,
    OutboxEvent,
    connection,
    publisher,
    publisher_enabled,
)
from .schemas import (
    Connector,
    ConnectorCreate,
    HealthReport,
    Invocation,
    InvocationCreate,
    ValidationReport,
)
from .security.redaction import redact_config

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

__all__ = ["Connector", "ConnectorCreate", "app"]

ADAPTERS = build_registry()
EXPECTED_MIGRATION = os.getenv("EXPECTED_MIGRATION", "0006")

# /internal/* bypasses APISIX, so it carries no OIDC token. It is protected
# by a shared service credential compared in constant time, in addition to
# any NetworkPolicy: network position alone is not authentication, and a
# compromised in-cluster workload would otherwise inherit full connector
# invocation rights.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")


def require_internal_caller(
    x_internal_service_token: str = Header(default=""),
) -> None:
    if not INTERNAL_SERVICE_TOKEN:
        # Unset means the internal surface is disabled rather than open.
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "INTERNAL_SURFACE_DISABLED",
                "message": "INTERNAL_SERVICE_TOKEN is not configured",
            },
        )
    if not secrets.compare_digest(x_internal_service_token, INTERNAL_SERVICE_TOKEN):
        raise HTTPException(
            status_code=401,
            detail={"error_code": "INTERNAL_AUTH_FAILED"},
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("outbox.duplicate_window_utilisation=%.1f%%", WINDOW_UTILISATION * 100)
    logger.info("adapters.registered %s", registry_snapshot(ADAPTERS))
    await connection.connect()
    if publisher_enabled():
        publisher.start()
    else:
        logger.warning("outbox.publisher_disabled service=connector-registry")
    yield
    await publisher.stop()
    await connection.close()
    await engine.dispose()


app = FastAPI(
    title="FAVL Connector Registry",
    version="0.4.0",
    lifespan=lifespan,
)


def _to_schema(record: ConnectorRecord) -> Connector:
    # model_validate rather than the constructor: `kind` and `status` are
    # plain columns constrained by CHECK constraints, and `base_url` is text.
    # Pydantic owns narrowing those to their literal and URL types; asserting
    # the narrow types here would be a claim the ORM cannot back.
    return Connector.model_validate(
        {
            "id": str(record.id),
            "name": record.name,
            "kind": record.kind,
            "base_url": record.base_url,
            "scopes": list(record.scopes),
            # Redacted on every read path: a credential that slipped past
            # validation must not be republished through the API or
            # into an outbox event.
            "config": redact_config(dict(record.config or {})),
            "status": record.status,
            "version": record.version,
            "created_at": record.created_at,
            "deletion_requested_at": record.deletion_requested_at,
            "deleted_at": record.deleted_at,
            "supports_idempotency": record.supports_idempotency,
            "idempotency_mode": record.idempotency_mode,
        }
    )


def _http_status_for(record: InvocationRecord) -> int:
    """HTTP status for a terminal invocation state.

    Shared by the fresh and replay branches. When these diverged, replaying
    a failed idempotency key returned 200 while the original attempt had
    returned 502 — a caller retrying after a failure would have concluded it
    had succeeded.
    """
    if record.status == InvocationStatus.SUCCEEDED.value:
        return 200
    if record.status == InvocationStatus.TIMED_OUT.value:
        return 504
    if record.status in (
        InvocationStatus.ACCEPTED.value,
        InvocationStatus.RUNNING.value,
    ):
        return 202
    return 502


def _invocation_schema(record: InvocationRecord) -> Invocation:
    duration = None
    if record.started_at and record.completed_at:
        duration = (record.completed_at - record.started_at).total_seconds() * 1000
    return Invocation(
        id=str(record.id),
        connector_id=str(record.connector_id),
        connector_version=record.connector_version,
        adapter_kind=record.adapter_kind,
        idempotency_key=record.idempotency_key,
        actor_id=record.actor_id,
        tenant_id=record.tenant_id,
        operation=record.operation,
        status=record.status,
        attempt=record.attempt,
        deadline_at=record.deadline_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        provider_request_id=record.provider_request_id,
        error_code=record.error_code,
        error_detail=record.error_detail,
        retryable=record.retryable,
        trace_id=record.trace_id,
        output=record.output,
        duration_ms=duration,
    )


# --------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------- #


@app.get("/livez")
@app.get("/health/live")
async def live() -> dict[str, str]:
    """Liveness: is this process still running its event loop?

    Deliberately checks no external dependency. A liveness probe that fails
    when PostgreSQL or NATS is down turns a dependency outage into a
    cluster-wide restart storm, which is strictly worse than a degraded but
    running service. Dependency state belongs in /readyz.
    """
    return {"status": "live"}


@app.get("/health/ready")
async def public_ready() -> Response:
    """Reduced public readiness.

    Booleans only. The detailed body — dependency exception strings, outbox
    counters, registered adapters — is reconnaissance material for an
    unauthenticated caller, and /health/* is the one unauthenticated route on
    the gateway. Operators read /readyz, which is cluster-internal.
    """
    detail = await _readiness()
    return JSONResponse(
        {
            "status": detail["status"],
            "database_connected": detail["database_connected"],
            "nats_connected": detail["nats_connected"],
            "migrations_current": detail["migrations_current"],
        },
        status_code=200 if detail["status"] == "ready" else 503,
    )


@app.get("/readyz")
async def ready() -> Response:
    detail = await _readiness()
    return JSONResponse(detail, status_code=200 if detail["status"] == "ready" else 503)


async def _readiness() -> dict[str, Any]:
    db_ok = True
    migrations_current = False
    outbox: dict[str, Any] = {}
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
            head = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
            migrations_current = head == EXPECTED_MIGRATION
    except SQLAlchemyError as exc:
        db_ok = False
        logger.error("health.database_unreachable error=%s", exc)

    if db_ok:
        try:
            stats = await publisher.stats()
            outbox = {
                "pending": stats.pending,
                "dead": stats.dead,
                "published": stats.published,
                "oldest_pending_age_seconds": stats.oldest_pending_age_seconds,
                "publisher_running": publisher.running,
            }
        except SQLAlchemyError as exc:
            logger.error("health.outbox_stats_failed error=%s", exc)

    nats_ok = connection.ready
    healthy = db_ok and nats_ok and migrations_current
    body: dict[str, Any] = {
        "status": "ready" if healthy else "not_ready",
        "database_connected": db_ok,
        "nats_connected": nats_ok,
        "migrations_current": migrations_current,
        "required_stream_available": nats_ok,
        "adapters": ADAPTERS.kinds,
        "outbox": outbox,
    }
    if connection.last_error:
        body["nats_last_error"] = connection.last_error
    if publisher.last_error:
        body["outbox_last_error"] = publisher.last_error
    return body


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    """Internal only: not routed through APISIX."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --------------------------------------------------------------------- #
# connectors
# --------------------------------------------------------------------- #


@app.get("/v1/connectors", response_model=list[Connector])
async def list_connectors(
    include_deleted: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[Connector]:
    query = select(ConnectorRecord).order_by(ConnectorRecord.created_at)
    if not include_deleted:
        query = query.where(ConnectorRecord.status != ConnectorStatus.DELETED.value)
    result = await session.execute(query)
    return [_to_schema(record) for record in result.scalars()]


@app.get("/v1/connectors/{connector_id}", response_model=Connector)
async def get_connector(
    connector_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Connector:
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return _to_schema(record)


@app.post(
    "/v1/connectors", response_model=Connector, status_code=status.HTTP_201_CREATED
)
async def create_connector(
    payload: ConnectorCreate, session: AsyncSession = Depends(get_session)
) -> Connector:
    if not ADAPTERS.has(payload.kind):
        # Fail at registration, not at first invocation.
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "UNKNOWN_KIND",
                "message": f"no adapter registered for kind '{payload.kind}'",
                "registered_kinds": ADAPTERS.kinds,
            },
        )

    config = dict(payload.config)
    if payload.base_url and "base_url" not in config:
        config["base_url"] = str(payload.base_url)

    report = await ADAPTERS.get(payload.kind).validate_config(config)
    if not report.valid:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "CONFIG_INVALID", "errors": report.errors},
        )

    record = ConnectorRecord(
        name=payload.name,
        kind=payload.kind,
        base_url=str(payload.base_url) if payload.base_url else None,
        scopes=payload.scopes,
        config=config,
        status=payload.status.value,
        supports_idempotency=report.supports_idempotency,
        idempotency_mode=report.idempotency_mode.value,
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"Connector named '{payload.name}' already exists"
        ) from None

    connector = _to_schema(record)
    enqueue(
        session,
        OutboxEvent,
        subject="connector.created",
        payload=connector.model_dump(mode="json"),
        aggregate_type="connector",
        aggregate_id=str(record.id),
        aggregate_version=record.version,
    )
    await session.commit()
    return connector


@app.post("/v1/connectors/{connector_id}/validate", response_model=ValidationReport)
async def validate_connector(
    connector_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> ValidationReport:
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    report = await ADAPTERS.get(record.kind).validate_config(dict(record.config or {}))
    return ValidationReport(
        valid=report.valid,
        errors=report.errors,
        supports_idempotency=report.supports_idempotency,
        idempotency_mode=report.idempotency_mode.value,
    )


@app.post("/v1/connectors/{connector_id}/health", response_model=HealthReport)
async def connector_health(
    connector_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> HealthReport:
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    context = ConnectorContext(
        connector_id=str(record.id),
        connector_version=record.version,
        kind=record.kind,
        config=dict(record.config or {}),
    )
    result = await ADAPTERS.get(record.kind).health_check(context)
    return HealthReport(
        healthy=result.healthy,
        detail=result.detail,
        checked_at=result.checked_at,
        latency_ms=result.latency_ms,
    )


# response_model=None is required: FastAPI otherwise infers NoneType from the
# `-> None` annotation, which is truthy, and rejects it against a 204.
@app.delete(
    "/v1/connectors/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def request_connector_deletion(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(current_identity),
) -> Response:
    """Soft deletion.

    The row is retained: audit records, invocation history and provenance
    references depend on the connector identity continuing to exist. Physical
    deletion becomes a later privileged operation with its own preconditions.
    The caller still sees 204, since the transition completes synchronously.
    """
    record = await session.get(ConnectorRecord, connector_id, with_for_update=True)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    if record.status == ConnectorStatus.DELETED.value:
        raise HTTPException(
            status_code=410,
            detail={"error_code": "CONNECTOR_GONE", "message": "Connector is deleted"},
        )
    if record.status == ConnectorStatus.DELETION_REQUESTED.value:
        # Already in the requested state; repeating is a no-op, not an error.
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    record.status = ConnectorStatus.DELETION_REQUESTED.value
    record.deletion_requested_at = datetime.now(UTC)
    record.deleted_by = caller.actor_id
    record.version += 1

    enqueue(
        session,
        OutboxEvent,
        subject="connector.deletion_requested",
        payload={
            "connector_id": str(connector_id),
            "name": record.name,
            "status": record.status,
            "requested_by": caller.actor_id,
            "requested_at": record.deletion_requested_at.isoformat(),
        },
        aggregate_type="connector",
        aggregate_id=str(connector_id),
        aggregate_version=record.version,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------- #
# invocation
# --------------------------------------------------------------------- #


@app.post("/v1/connectors/{connector_id}/invoke", response_model=None)
async def invoke_connector_v1(
    connector_id: uuid.UUID,
    payload: InvocationCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    caller: CallerIdentity = Depends(current_identity),
) -> Response:
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    try:
        inv.check_executable(record)
    except inv.ConnectorNotExecutable as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"error_code": exc.code.value, "status": exc.status},
        ) from None

    trace_id = request.headers.get("X-Request-ID") or request.headers.get("traceparent")

    invocation, replay = await inv.accept(
        session,
        record,
        operation=payload.operation,
        idempotency_key=payload.idempotency_key,
        actor_id=caller.actor_id,
        tenant_id=caller.tenant_id,
        timeout_seconds=payload.timeout_seconds,
        trace_id=trace_id,
    )

    if replay is not None:
        # Terminal replays carry the original attempt's status; in-flight
        # replays return 202 with the existing id rather than starting a
        # second provider-side effect.
        return JSONResponse(
            _invocation_schema(invocation).model_dump(mode="json"),
            status_code=_http_status_for(invocation),
        )

    invocation = await inv.execute(
        session, ADAPTERS, record, invocation, payload.payload
    )
    return JSONResponse(
        _invocation_schema(invocation).model_dump(mode="json"),
        status_code=_http_status_for(invocation),
    )


@app.get("/v1/invocations", response_model=list[Invocation])
async def list_invocations(
    connector_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(current_tenant),
) -> list[Invocation]:
    """Newest first, keyset-paged on created_at.

    Offset paging would drift as new invocations arrive during a scroll,
    which is the normal case for an operational console.
    """
    # Always tenant-scoped. An invocation record carries actor, operation and
    # provider identifiers, so cross-tenant reads are a disclosure bug.
    query = (
        select(InvocationRecord)
        .where(InvocationRecord.tenant_id == tenant_id)
        .order_by(InvocationRecord.created_at.desc())
    )
    if connector_id is not None:
        query = query.where(InvocationRecord.connector_id == connector_id)
    if status_filter:
        query = query.where(InvocationRecord.status == status_filter)
    if before is not None:
        query = query.where(InvocationRecord.created_at < before)
    query = query.limit(max(1, min(limit, 200)))
    result = await session.execute(query)
    return [_invocation_schema(record) for record in result.scalars()]


@app.get("/v1/invocations/{invocation_id}", response_model=Invocation)
async def get_invocation(
    invocation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    tenant_id: str = Depends(current_tenant),
) -> Invocation:
    record = await session.get(InvocationRecord, invocation_id)
    # 404 rather than 403 on a tenant mismatch: confirming that an id exists
    # in another tenant is itself a disclosure.
    if record is None or record.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Invocation not found")
    return _invocation_schema(record)


@app.post("/internal/connectors/{connector_id}/invoke")
async def invoke_connector_internal(
    connector_id: uuid.UUID,
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_internal_caller),
) -> dict[str, Any]:
    """Orchestrator-facing path. Same runtime, no echo fallback."""
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    try:
        inv.check_executable(record)
    except inv.ConnectorNotExecutable as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"error_code": exc.code.value, "status": exc.status},
        ) from None

    agent_id = str(payload.get("agent_id", "unknown"))
    body = payload.get("input", {}) or {}
    # Derived so an orchestrator retry of the same agent call collapses onto
    # the same invocation instead of hitting the provider twice.
    digest = uuid.uuid5(uuid.NAMESPACE_OID, repr(sorted(body.items())))
    key = f"agent:{agent_id}:{digest}"

    invocation, replay = await inv.accept(
        session,
        record,
        operation=str((record.config or {}).get("operation", "")),
        idempotency_key=key,
        actor_id=f"agent:{agent_id}",
        tenant_id="default",
        timeout_seconds=float((record.config or {}).get("timeout_seconds", 15.0)),
        trace_id=None,
    )
    if replay is None:
        invocation = await inv.execute(session, ADAPTERS, record, invocation, body)

    return {
        "invocation_id": str(invocation.id),
        "connector_id": str(connector_id),
        "connector_kind": record.kind,
        "status": invocation.status,
        "error_code": invocation.error_code,
        "retryable": invocation.retryable,
        "output": invocation.output,
    }


@app.exception_handler(UnknownAdapterKind)
async def unknown_kind_handler(
    request: Request, exc: UnknownAdapterKind
) -> JSONResponse:
    return JSONResponse(
        {"error_code": "UNKNOWN_KIND", "kind": exc.kind, "registered_kinds": exc.known},
        status_code=422,
    )
