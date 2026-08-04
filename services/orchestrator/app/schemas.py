"""Public API contract.

Kept free of database and broker imports so the contract can be tested
without a running Postgres or NATS.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentCreate(BaseModel):
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(default="", max_length=500)
    connector_ids: list[str] = Field(default_factory=list)


class Agent(AgentCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime


class Invocation(BaseModel):
    agent_id: str
    input: dict[str, Any]
