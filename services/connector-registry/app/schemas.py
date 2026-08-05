"""Public API contract, free of database imports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .lifecycle import CREATABLE_STATES, ConnectorState

ConnectorKind = Literal["http", "mcp", "webhook", "internal"]

# Config keys that would carry a literal secret. Rejected at the edge so a
# secret never reaches the database, an event, or a log line.
FORBIDDEN_CONFIG_KEYS = frozenset(
    {"signing_secret", "api_key", "password", "token", "client_secret", "private_key"}
)

# Settings that would WIDEN outbound reach. They are deployment policy, read
# from the operator environment. Accepting them here would let the author of
# a destination authorise the gateway to reach it — the connector equivalent
# of marking your own homework.
OPERATOR_ONLY_CONFIG_KEYS = frozenset(
    {"allow_private_addresses", "allowed_schemes", "allow_plaintext_acknowledged"}
)


# One lifecycle enum for the whole service. The API schema previously kept
# its own copy, so the OpenAPI contract advertised five states while the
# database accepted ten.
ConnectorStatusSchema = ConnectorState


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    kind: ConnectorKind
    base_url: HttpUrl | None = None
    scopes: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    status: ConnectorStatusSchema = ConnectorStatusSchema.ENABLED

    @field_validator("status")
    @classmethod
    def _only_creatable_states(
        cls, value: ConnectorStatusSchema
    ) -> ConnectorStatusSchema:
        """Creation has no source state, so the machine cannot police it here.

        Without this, a caller could create a connector already `revoked` or
        `archived` — skipping the reason those states require and producing a
        first audit entry that contradicts the state it describes.
        """
        if value not in CREATABLE_STATES:
            raise ValueError(
                f"a connector cannot be created in state '{value.value}'; "
                f"creatable states are "
                f"{sorted(s.value for s in CREATABLE_STATES)}"
            )
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

    @field_validator("config")
    @classmethod
    def _no_privilege_escalation(cls, value: dict[str, Any]) -> dict[str, Any]:
        escalating = sorted(OPERATOR_ONLY_CONFIG_KEYS.intersection(value))
        if escalating:
            raise ValueError(
                f"config may not set operator-controlled keys: {escalating}. "
                "Private addressing and permitted schemes are deployment "
                "settings; a connector cannot widen its own reach."
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
    revoked_at: datetime | None = None
    archived_at: datetime | None = None
    credentials_rotated_at: datetime | None = None
    # The reason recorded with the most recent transition. The full history,
    # with an actor per entry, is at GET /v1/connectors/{id}/audit — this is
    # the answer to "why is it in this state right now", not a substitute for
    # the trail.
    state_reason: str | None = None
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


class TransitionRequest(BaseModel):
    """Body for every lifecycle transition endpoint.

    `reason` is required by the state machine for suspensions, revocations and
    deletions — see `lifecycle.Transition.requires_reason`. It is optional here
    and enforced there, so the requirement lives in one place.
    """

    reason: str | None = Field(default=None, max_length=1024)
    idempotency_key: str | None = Field(default=None, max_length=255)


class AuditEntry(BaseModel):
    """One immutable lifecycle transition record."""

    id: uuid.UUID
    connector_id: uuid.UUID
    from_state: str
    to_state: str
    event: str
    actor_id: str
    reason: str | None
    aggregate_version: int
    recorded_at: datetime
