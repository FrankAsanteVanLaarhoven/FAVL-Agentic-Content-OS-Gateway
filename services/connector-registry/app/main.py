from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal, engine, get_session
from .models import ConnectorRecord
from .schemas import Connector, ConnectorCreate

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

__all__ = ["app", "Connector", "ConnectorCreate"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title="FAVL Connector Registry",
    version="0.2.0",
    lifespan=lifespan,
)


def _to_schema(record: ConnectorRecord) -> Connector:
    return Connector(
        id=str(record.id),
        name=record.name,
        kind=record.kind,
        base_url=record.base_url,
        scopes=list(record.scopes),
        enabled=record.enabled,
        created_at=record.created_at,
    )


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready")
async def ready() -> dict[str, Any]:
    db_ok = True
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        db_ok = False
        logger.error("health.database_unreachable error=%s", exc)

    return {
        "status": "ready" if db_ok else "degraded",
        "database_connected": db_ok,
    }


@app.get("/v1/connectors", response_model=list[Connector])
async def list_connectors(
    session: AsyncSession = Depends(get_session),
) -> list[Connector]:
    result = await session.execute(
        select(ConnectorRecord).order_by(ConnectorRecord.created_at)
    )
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
    record = ConnectorRecord(
        name=payload.name,
        kind=payload.kind,
        base_url=str(payload.base_url) if payload.base_url else None,
        scopes=payload.scopes,
        enabled=payload.enabled,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"Connector named '{payload.name}' already exists"
        ) from None
    await session.refresh(record)
    return _to_schema(record)


# response_model=None is required: FastAPI otherwise infers NoneType from the
# `-> None` annotation, which is truthy, and rejects it against a 204.
@app.delete(
    "/v1/connectors/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_connector(
    connector_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    await session.delete(record)
    await session.commit()


@app.post("/internal/connectors/{connector_id}/invoke")
async def invoke_connector(
    connector_id: uuid.UUID,
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await session.get(ConnectorRecord, connector_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    if not record.enabled:
        raise HTTPException(status_code=409, detail="Connector disabled")

    # Still a stub: no adapter dispatches on `kind` yet. Secrets must be
    # resolved from a secret manager, never stored in connector records.
    return {
        "connector_id": str(connector_id),
        "connector_kind": record.kind,
        "accepted": True,
        "input_keys": sorted(payload.keys()),
    }
