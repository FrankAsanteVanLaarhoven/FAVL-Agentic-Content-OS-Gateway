from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from favl_outbox import enqueue
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from . import invocations as inv
from .adapters.base import ConnectorContext, InvocationStatus
from .adapters.registry import UnknownAdapterKind, build_registry, registry_snapshot
from .db import SessionLocal, engine, get_session
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

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

__all__ = ["app", "Connector", "ConnectorCreate"]

ADAPTERS = build_registry()
EXPECTED_MIGRATION = os.getenv("EXPECTED_MIGRATION", "0004")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    return Connector(
        id=str(record.id),
        name=record.name,
        kind=record.kind,
        base_url=record.base_url,
        scopes=list(record.scopes),
        config=dict(record.config or {}),
        status=record.status,
        version=record.version,
        created_at=record.created_at,
        deletion_requested_at=record.deletion_requested_at,
        deleted_at=record.deleted_at,
    )


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


@app.get("/readyz")
@app.get("/health/ready")
async def ready() -> Response:
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
    # Non-200 when unready: orchestrators routinely inspect only the code.
    return JSONResponse(body, status_code=200 if healthy else 503)


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
    connector.supports_idempotency = report.supports_idempotency
    connector.idempotency_mode = report.idempotency_mode.value
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
    x_actor_id: str = Header(default="unknown"),
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
    record.deletion_requested_at = datetime.now(timezone.utc)
    record.deleted_by = x_actor_id
    record.version += 1

    enqueue(
        session,
        OutboxEvent,
        subject="connector.deletion_requested",
        payload={
            "connector_id": str(connector_id),
            "name": record.name,
            "status": record.status,
            "requested_by": x_actor_id,
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
    x_actor_id: str = Header(default="anonymous"),
    x_tenant_id: str = Header(default="default"),
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
        actor_id=x_actor_id,
        tenant_id=x_tenant_id,
        timeout_seconds=payload.timeout_seconds,
        trace_id=trace_id,
    )

    if replay is not None:
        if replay.in_flight:
            # Still running: hand back the existing id rather than starting a
            # second provider-side effect.
            return JSONResponse(
                _invocation_schema(invocation).model_dump(mode="json"),
                status_code=202,
            )
        # Terminal: return the stored result verbatim, success or failure.
        return JSONResponse(
            _invocation_schema(invocation).model_dump(mode="json"), status_code=200
        )

    invocation = await inv.execute(
        session, ADAPTERS, record, invocation, payload.payload
    )
    if invocation.status == InvocationStatus.SUCCEEDED.value:
        http_status = 200
    elif invocation.status == InvocationStatus.TIMED_OUT.value:
        http_status = 504
    else:
        http_status = 502
    return JSONResponse(
        _invocation_schema(invocation).model_dump(mode="json"), status_code=http_status
    )


@app.get("/v1/invocations/{invocation_id}", response_model=Invocation)
async def get_invocation(
    invocation_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Invocation:
    record = await session.get(InvocationRecord, invocation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Invocation not found")
    return _invocation_schema(record)


@app.post("/internal/connectors/{connector_id}/invoke")
async def invoke_connector_internal(
    connector_id: uuid.UUID,
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_session),
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
