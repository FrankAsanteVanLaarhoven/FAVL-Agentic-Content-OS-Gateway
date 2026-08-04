from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from favl_outbox import enqueue
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal, engine, get_session
from .models import AgentRecord
from .outbox import OutboxEvent, connection, publisher, publisher_enabled
from .outbox import WINDOW_UTILISATION
from .schemas import Agent, AgentCreate, Invocation

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

__all__ = ["app", "Agent", "AgentCreate", "Invocation"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The invariant is enforced at import, before logging is configured, so
    # the result is restated here where it is actually visible.
    logger.info(
        "outbox.duplicate_window_utilisation=%.1f%%", WINDOW_UTILISATION * 100
    )
    await connection.connect()
    if publisher_enabled():
        # Pending rows left by a previous process are picked up here; no
        # separate recovery path is needed.
        publisher.start()
    else:
        logger.warning("outbox.publisher_disabled service=orchestrator")
    yield
    await publisher.stop()
    await connection.close()
    await engine.dispose()


app = FastAPI(
    title="FAVL Agent Orchestrator",
    version="0.3.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _to_schema(record: AgentRecord) -> Agent:
    return Agent(
        id=str(record.id),
        name=record.name,
        description=record.description,
        connector_ids=list(record.connector_ids),
        created_at=record.created_at,
    )


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
async def ready() -> dict[str, Any]:
    """Readiness reports each dependency plus outbox backlog separately."""
    db_ok = True
    outbox: dict[str, Any] = {}
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
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
    body: dict[str, Any] = {
        # A backlog does not make the service unready: writes are still
        # accepted and durable. Only a dead dependency does.
        "status": "ready" if (db_ok and nats_ok) else "degraded",
        "database_connected": db_ok,
        "nats_connected": nats_ok,
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


@app.get("/v1/agents", response_model=list[Agent])
async def list_agents(session: AsyncSession = Depends(get_session)) -> list[Agent]:
    result = await session.execute(select(AgentRecord).order_by(AgentRecord.created_at))
    return [_to_schema(record) for record in result.scalars()]


@app.get("/v1/agents/{agent_id}", response_model=Agent)
async def get_agent(
    agent_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Agent:
    record = await session.get(AgentRecord, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _to_schema(record)


@app.post("/v1/agents", response_model=Agent, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate, session: AsyncSession = Depends(get_session)
) -> Agent:
    record = AgentRecord(
        name=payload.name,
        description=payload.description,
        connector_ids=payload.connector_ids,
    )
    session.add(record)
    # Flush to obtain the generated id and surface a name collision before an
    # outbox row is staged for an agent that will not exist.
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"Agent named '{payload.name}' already exists"
        ) from None

    agent = _to_schema(record)
    enqueue(
        session,
        OutboxEvent,
        subject="agent.created",
        payload=agent.model_dump(mode="json"),
        aggregate_type="agent",
        aggregate_id=str(record.id),
        aggregate_version=record.version,
    )
    # One commit covers the agent and its event. A crash on either side of
    # this line leaves both present or both absent, never one alone.
    await session.commit()
    return agent


# response_model=None is required: FastAPI otherwise infers NoneType from the
# `-> None` annotation, which is truthy, and rejects it against a 204.
@app.delete(
    "/v1/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_agent(
    agent_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    record = await session.get(AgentRecord, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await session.delete(record)
    enqueue(
        session,
        OutboxEvent,
        subject="agent.deleted",
        payload={"agent_id": str(agent_id), "name": record.name},
        aggregate_type="agent",
        aggregate_id=str(agent_id),
        # The row is going away; the delete is the next version of it.
        aggregate_version=record.version + 1,
    )
    await session.commit()


@app.post("/v1/agents/{agent_id}/invoke")
async def invoke_agent(
    agent_id: uuid.UUID,
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await session.get(AgentRecord, agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    registry_url = os.getenv("CONNECTOR_REGISTRY_URL", "http://localhost:8001")
    outputs: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for connector_id in record.connector_ids:
            try:
                response = await client.post(
                    f"{registry_url}/internal/connectors/{connector_id}/invoke",
                    json={"agent_id": str(agent_id), "input": payload},
                )
            except httpx.HTTPError as exc:
                outputs.append(
                    {
                        "connector_id": connector_id,
                        "status": "failed",
                        "detail": f"registry unreachable: {exc}",
                    }
                )
                continue

            if response.status_code >= 400:
                outputs.append(
                    {
                        "connector_id": connector_id,
                        "status": "failed",
                        "detail": response.text,
                    }
                )
            else:
                outputs.append(
                    {
                        "connector_id": connector_id,
                        "status": "completed",
                        "output": response.json(),
                    }
                )

    result = {"agent_id": str(agent_id), "outputs": outputs}
    enqueue(
        session,
        OutboxEvent,
        subject="agent.invoked",
        payload=result,
        aggregate_type="agent",
        aggregate_id=str(agent_id),
        aggregate_version=record.version,
    )
    await session.commit()
    return result
