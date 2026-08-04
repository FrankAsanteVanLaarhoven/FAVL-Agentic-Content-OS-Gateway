"""Internal service adapter.

Addresses a service by registered name, never by caller-supplied URL. A
connector config carrying `{"url": "http://anything.internal"}` would make
this an SSRF primitive pointed at our own network; a registry lookup cannot
reach anything an operator has not registered.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from ..security import outbound
from ..security.ssrf import OutboundPolicy, SSRFBlocked
from .base import (
    ConnectorContext,
    ErrorCode,
    HealthResult,
    IdempotencyMode,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
    ValidationResult,
)

logger = logging.getLogger(__name__)


def _load_registry() -> dict[str, str]:
    """`name=base_url,name=base_url` from the environment.

    Operator-controlled. Nothing here comes from a connector record.
    """
    raw = os.getenv("INTERNAL_SERVICE_REGISTRY", "")
    registry: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, base = entry.partition("=")
        registry[name.strip()] = base.strip().rstrip("/")
    return registry


class InternalAdapter:
    kind = "internal"

    def __init__(self, registry: dict[str, str] | None = None) -> None:
        self._registry = registry if registry is not None else _load_registry()

    def _policy(self, host: str) -> OutboundPolicy:
        # Internal destinations are private by definition, so private
        # addresses are permitted — but only for a registered service, and
        # metadata endpoints stay blocked inside resolve_and_validate.
        return OutboundPolicy(
            allowed_hosts=frozenset({host}),
            allowed_schemes=("http", "https"),
            allow_private_addresses=True,
            max_redirects=0,
            max_response_bytes=int(os.getenv("INTERNAL_MAX_RESPONSE_BYTES", "1048576")),
        )

    async def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        errors = []
        service = config.get("service")
        if not service:
            errors.append("config.service is required")
        elif service not in self._registry:
            errors.append(
                f"service '{service}' is not registered; "
                f"known services: {sorted(self._registry) or 'none'}"
            )
        if not config.get("operation"):
            errors.append("config.operation is required")
        if "url" in config:
            errors.append("config.url is not permitted; address services by name")
        if errors:
            return ValidationResult.failed(*errors)
        return ValidationResult.ok(
            supports_idempotency=True,
            idempotency_mode=IdempotencyMode.GATEWAY_DEDUP_ONLY,
        )

    def _target(self, context: ConnectorContext) -> tuple[str, str]:
        service = context.config.get("service", "")
        base = self._registry.get(service)
        if base is None:
            raise SSRFBlocked("service_not_registered", service)
        return base, context.config.get("operation", "")

    async def health_check(self, context: ConnectorContext) -> HealthResult:
        started = datetime.now(timezone.utc)
        try:
            base, _ = self._target(context)
        except SSRFBlocked as exc:
            return HealthResult(False, exc.reason, started)

        url = f"{base}/health/live"
        try:
            async with httpx.AsyncClient() as client:
                response = await outbound.request(
                    client,
                    "GET",
                    url,
                    self._policy(httpx.URL(url).host),
                    connect_timeout=2.0,
                    read_timeout=3.0,
                    total_timeout=5.0,
                )
        except Exception as exc:
            return HealthResult(False, f"{type(exc).__name__}: {exc}"[:200], started)

        latency = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return HealthResult(
            healthy=200 <= response.status_code < 300,
            detail=f"HTTP {response.status_code}",
            checked_at=started,
            latency_ms=latency,
        )

    async def invoke(
        self, request: InvocationRequest, context: ConnectorContext
    ) -> InvocationResult:
        started = datetime.now(timezone.utc)
        try:
            base, operation = self._target(context)
        except SSRFBlocked as exc:
            return InvocationResult.failure(
                ErrorCode.SERVICE_NOT_REGISTERED,
                exc.detail,
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )

        remaining = request.seconds_remaining(started)
        if remaining <= 0:
            return InvocationResult.failure(
                ErrorCode.DEADLINE_EXCEEDED,
                "deadline passed before dispatch",
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )

        url = f"{base}/{operation.lstrip('/')}"
        try:
            async with httpx.AsyncClient() as client:
                response = await outbound.request(
                    client,
                    "POST",
                    url,
                    self._policy(httpx.URL(url).host),
                    json_body=request.payload,
                    headers={
                        "X-FAVL-Invocation-ID": request.invocation_id,
                        "X-FAVL-Idempotency-Key": request.idempotency_key,
                        "Content-Type": "application/json",
                    },
                    connect_timeout=min(2.0, remaining),
                    read_timeout=remaining,
                    total_timeout=remaining,
                )
        except httpx.TimeoutException as exc:
            return InvocationResult.failure(
                ErrorCode.UPSTREAM_TIMEOUT,
                str(exc),
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except outbound.ResponseTooLarge as exc:
            return InvocationResult.failure(
                ErrorCode.RESPONSE_TOO_LARGE,
                str(exc),
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except SSRFBlocked as exc:
            return InvocationResult.failure(
                ErrorCode.SSRF_BLOCKED,
                exc.reason,
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except httpx.HTTPError as exc:
            return InvocationResult.failure(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"{type(exc).__name__}: {exc}",
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )

        completed = datetime.now(timezone.utc)
        return _classify(response, started, completed, service_hint=operation)


def _classify(
    response: outbound.OutboundResponse,
    started: datetime,
    completed: datetime,
    service_hint: str = "",
) -> InvocationResult:
    import json

    if 200 <= response.status_code < 300:
        try:
            output = json.loads(response.body) if response.body else {}
        except ValueError:
            output = {"raw": response.body.decode("utf-8", "replace")[:4000]}
        return InvocationResult(
            status=InvocationStatus.SUCCEEDED,
            output=output if isinstance(output, dict) else {"result": output},
            provider_request_id=response.provider_request_id,
            started_at=started,
            completed_at=completed,
            audit_metadata={
                "http_status": response.status_code,
                "redirects": response.redirects,
                "operation": service_hint,
            },
        )

    # 4xx is the caller's fault and will fail identically on retry.
    code = (
        ErrorCode.UPSTREAM_CLIENT_ERROR
        if 400 <= response.status_code < 500
        else ErrorCode.UPSTREAM_ERROR
    )
    return InvocationResult.failure(
        code,
        f"HTTP {response.status_code}",
        started_at=started,
        completed_at=completed,
        provider_request_id=response.provider_request_id,
        audit_metadata={"http_status": response.status_code},
    )
