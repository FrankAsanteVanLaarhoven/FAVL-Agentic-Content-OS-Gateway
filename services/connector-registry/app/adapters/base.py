"""The connector adapter contract.

Validation, health and invocation are separate operations so a connector can
be checked before it is trusted with traffic, and so health can be polled
without side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ErrorCode(str, Enum):
    """Stable, classifiable failure reasons. Never a raw provider string."""

    CONFIG_INVALID = "CONFIG_INVALID"
    UNKNOWN_KIND = "UNKNOWN_KIND"
    CONNECTOR_DISABLED = "CONNECTOR_DISABLED"
    CONNECTOR_DELETION_PENDING = "CONNECTOR_DELETION_PENDING"
    CONNECTOR_GONE = "CONNECTOR_GONE"
    SECRET_NOT_FOUND = "SECRET_NOT_FOUND"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_CLIENT_ERROR = "UPSTREAM_CLIENT_ERROR"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    CONTENT_TYPE_REJECTED = "CONTENT_TYPE_REJECTED"
    TOO_MANY_REDIRECTS = "TOO_MANY_REDIRECTS"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    SERVICE_NOT_REGISTERED = "SERVICE_NOT_REGISTERED"
    OPERATION_NOT_SUPPORTED = "OPERATION_NOT_SUPPORTED"


# Failures worth retrying. Everything else is terminal: retrying a 400 or a
# blocked host only burns quota and hides the real problem.
RETRYABLE_CODES = frozenset(
    {
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.UPSTREAM_ERROR,
    }
)


class IdempotencyMode(str, Enum):
    """How far a given adapter can actually honour an idempotency key.

    Exposed on the connector so callers are not misled: the gateway cannot
    promise exactly-once side effects at a provider that has no notion of it.
    """

    # Provider accepts an idempotency key and enforces it.
    PROVIDER_KEY = "provider_key"
    # Operation has no side effects, so repetition is harmless.
    READ_ONLY = "read_only"
    # Gateway deduplicates locally only. A crash after the provider accepts
    # but before the local result commits can still double-apply upstream.
    GATEWAY_DEDUP_ONLY = "gateway_dedup_only"
    UNSUPPORTED = "unsupported"


class InvocationStatus(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED_TERMINAL,
        InvocationStatus.TIMED_OUT,
        InvocationStatus.CANCELLED,
    }
)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    # Declared here so the API can publish the connector's real guarantee.
    supports_idempotency: bool = False
    idempotency_mode: IdempotencyMode = IdempotencyMode.UNSUPPORTED

    @classmethod
    def ok(
        cls,
        *,
        supports_idempotency: bool = False,
        idempotency_mode: IdempotencyMode = IdempotencyMode.UNSUPPORTED,
    ) -> ValidationResult:
        return cls(True, [], supports_idempotency, idempotency_mode)

    @classmethod
    def failed(cls, *errors: str) -> ValidationResult:
        return cls(False, list(errors))


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    detail: str = ""
    checked_at: datetime | None = None
    latency_ms: float | None = None


@dataclass(frozen=True)
class ConnectorContext:
    """Everything the adapter may know about the connector it is serving.

    Secrets are referenced, never inlined: `secret_refs` holds identifiers
    the adapter resolves through the injected resolver at the moment of use,
    so a raw value never sits in a context object that might be logged.
    """

    connector_id: str
    connector_version: int
    kind: str
    config: dict[str, Any]
    secret_refs: dict[str, str] = field(default_factory=dict)
    tenant_id: str = "default"


@dataclass(frozen=True)
class InvocationRequest:
    invocation_id: str
    connector_id: str
    operation: str
    payload: dict[str, Any]
    idempotency_key: str
    actor_id: str
    tenant_id: str
    deadline_at: datetime
    trace_id: str | None = None
    attempt: int = 1
    requested_scopes: tuple[str, ...] = ()

    def seconds_remaining(self, now: datetime) -> float:
        return max(0.0, (self.deadline_at - now).total_seconds())


@dataclass(frozen=True)
class InvocationResult:
    status: InvocationStatus
    output: dict[str, Any] = field(default_factory=dict)
    error_code: ErrorCode | None = None
    error_detail: str = ""
    retryable: bool = False
    provider_request_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    audit_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None

    @classmethod
    def failure(
        cls,
        code: ErrorCode,
        detail: str = "",
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        provider_request_id: str | None = None,
        audit_metadata: dict[str, Any] | None = None,
    ) -> InvocationResult:
        retryable = code in RETRYABLE_CODES
        return cls(
            status=(
                InvocationStatus.TIMED_OUT
                if code in (ErrorCode.UPSTREAM_TIMEOUT, ErrorCode.DEADLINE_EXCEEDED)
                else (
                    InvocationStatus.FAILED_RETRYABLE
                    if retryable
                    else InvocationStatus.FAILED_TERMINAL
                )
            ),
            error_code=code,
            error_detail=detail[:1000],
            retryable=retryable,
            started_at=started_at,
            completed_at=completed_at,
            provider_request_id=provider_request_id,
            audit_metadata=audit_metadata or {},
        )


@runtime_checkable
class ConnectorAdapter(Protocol):
    kind: str

    async def validate_config(self, config: dict[str, Any]) -> ValidationResult: ...

    async def health_check(self, context: ConnectorContext) -> HealthResult: ...

    async def invoke(
        self, request: InvocationRequest, context: ConnectorContext
    ) -> InvocationResult: ...
