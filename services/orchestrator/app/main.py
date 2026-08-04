from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal, engine, get_session
from .events import publisher
from .models import AgentRecord
from .schemas import Agent, AgentCreate, Invocation

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

__all__ = ["app", "Agent", "AgentCreate", "Invocation"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await publisher.connect()
    yield
    await publisher.close()
    await engine.dispose()


app = FastAPI(
    title="FAVL Agent Orchestrator",
    version="0.2.0",
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


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready")
async def ready() -> dict[str, Any]:
    """Readiness reflects both dependencies, not just the broker."""
    db_ok = True
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        db_ok = False
        logger.error("health.database_unreachable error=%s", exc)

    nats_ok = publisher.connected and publisher.stream_ready
    body: dict[str, Any] = {
        "status": "ready" if (db_ok and nats_ok) else "degraded",
        "database_connected": db_ok,
        "nats_connected": nats_ok,
    }
    if publisher.last_error:
        body["nats_last_error"] = publisher.last_error
    return body


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
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"Agent named '{payload.name}' already exists"
        ) from None
    await session.refresh(record)

    agent = _to_schema(record)
    await publisher.publish("agent.created", agent.model_dump(mode="json"))
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
    await session.commit()
    await publisher.publish("agent.deleted", {"agent_id": str(agent_id)})


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
    await publisher.publish("agent.invoked", result)
    return result
