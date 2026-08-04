"""Public API contract, free of database imports."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

ConnectorKind = Literal["http", "mcp", "webhook", "internal"]

# Config keys that would carry a literal secret. Rejected at the edge so a
# secret never reaches the database, an event, or a log line.
FORBIDDEN_CONFIG_KEYS = frozenset(
    {"signing_secret", "api_key", "password", "token", "client_secret", "private_key"}
)


class ConnectorStatusSchema(str, Enum):
    DRAFT = "draft"
    ENABLED = "enabled"
    DISABLED = "disabled"
    DELETION_REQUESTED = "deletion_requested"
    DELETED = "deleted"


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    kind: ConnectorKind
    base_url: HttpUrl | None = None
    scopes: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    status: ConnectorStatusSchema = ConnectorStatusSchema.ENABLED

    @field_validator("status")
    @classmethod
    def _no_terminal_status_on_create(
        cls, value: ConnectorStatusSchema
    ) -> ConnectorStatusSchema:
        if value in (
            ConnectorStatusSchema.DELETION_REQUESTED,
            ConnectorStatusSchema.DELETED,
        ):
            raise ValueError("a connector cannot be created in a deletion state")
        return value

    @field_validator("config")
    @classmethod
    def _no_literal_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        offending = sorted(FORBIDDEN_CONFIG_KEYS.intersection(value))
        if offending:
            raise ValueError(
                f"config may not contain literal secrets: {offending}. "
                "Use a reference such as env:NAME with a *_ref key."
            )
        return value


class Connector(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: ConnectorKind
    base_url: HttpUrl | None = None
    scopes: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    status: ConnectorStatusSchema
    version: int
    created_at: datetime
    deletion_requested_at: datetime | None = None
    deleted_at: datetime | None = None
    supports_idempotency: bool = False
    idempotency_mode: str = "unsupported"


class InvocationCreate(BaseModel):
    operation: str = Field(default="", max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=255)
    timeout_seconds: float = Field(default=15.0, gt=0, le=300)


class Invocation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    connector_id: str
    connector_version: int
    adapter_kind: str
    idempotency_key: str
    actor_id: str
    tenant_id: str
    operation: str
    status: str
    attempt: int
    deadline_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    retryable: bool | None = None
    trace_id: str | None = None
    output: dict[str, Any] | None = None
    duration_ms: float | None = None


class ValidationReport(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    supports_idempotency: bool = False
    idempotency_mode: str = "unsupported"


class HealthReport(BaseModel):
    healthy: bool
    detail: str = ""
    checked_at: datetime | None = None
    latency_ms: float | None = None
