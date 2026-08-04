"""Public API contract, free of database imports."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

ConnectorKind = Literal["http", "mcp", "webhook", "internal"]


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    kind: ConnectorKind
    base_url: HttpUrl | None = None
    scopes: list[str] = Field(default_factory=list)
    enabled: bool = True


class Connector(ConnectorCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
