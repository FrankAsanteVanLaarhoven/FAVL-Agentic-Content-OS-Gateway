"""Outbound HTTP adapter.

Every destination must be allowlisted, HTTPS by default, resolved to a public
address, and pinned to that address for the connection. Without those checks
this adapter would let anyone who can create a connector use the gateway's
network position to reach internal services and cloud metadata endpoints.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from ..security import outbound
from ..security.secrets import SecretNotFound, SecretResolver, is_secret_reference
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

DEFAULT_MAX_RESPONSE_BYTES = int(os.getenv("HTTP_MAX_RESPONSE_BYTES", "1048576"))


class HttpAdapter:
    kind = "http"

    def __init__(self, secret_resolver: SecretResolver) -> None:
        self._secrets = secret_resolver

    def _policy(self, config: dict[str, Any]) -> OutboundPolicy:
        allowed = config.get("allowed_hosts") or []
        return OutboundPolicy(
            allowed_hosts=frozenset(allowed),
            allowed_schemes=tuple(config.get("allowed_schemes", ("https",))),
            # Only an operator editing the connector can turn this on, and
            # metadata endpoints stay blocked regardless.
            allow_private_addresses=bool(config.get("allow_private_addresses", False)),
            max_redirects=int(config.get("max_redirects", 3)),
            max_response_bytes=int(
                config.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)
            ),
            allowed_content_types=tuple(
                config.get(
                    "allowed_content_types",
                    ("application/json", "application/problem+json", "text/plain"),
                )
            ),
        )

    async def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        base_url = config.get("base_url")
        if not base_url:
            errors.append("config.base_url is required")

        allowed_hosts = config.get("allowed_hosts")
        if not allowed_hosts:
            errors.append(
                "config.allowed_hosts is required; an unrestricted HTTP "
                "connector is an SSRF primitive"
            )

        schemes = tuple(config.get("allowed_schemes", ("https",)))
        if any(s not in ("http", "https") for s in schemes):
            errors.append("config.allowed_schemes may only contain http or https")
        if "http" in schemes and not config.get("allow_plaintext_acknowledged"):
            errors.append(
                "plaintext http requires config.allow_plaintext_acknowledged=true"
            )

        for key, value in (config.get("headers") or {}).items():
            if is_secret_reference(value):
                continue
            if key.lower() in ("authorization", "x-api-key"):
                errors.append(
                    f"header '{key}' must be a secret reference, not a literal value"
                )

        if base_url and allowed_hosts:
            try:
                # Static only: config validity must not depend on DNS, and
                # the address is revalidated and pinned at invocation time.
                from ..security.ssrf import validate_static

                validate_static(base_url, self._policy(config))
            except SSRFBlocked as exc:
                errors.append(f"base_url rejected: {exc.reason}")

        if errors:
            return ValidationResult.failed(*errors)

        # The gateway can dedupe locally, but cannot promise the provider
        # will. Callers see the real guarantee.
        mode = (
            IdempotencyMode.PROVIDER_KEY
            if config.get("idempotency_header")
            else IdempotencyMode.GATEWAY_DEDUP_ONLY
        )
        return ValidationResult.ok(supports_idempotency=True, idempotency_mode=mode)

    async def _headers(
        self, context: ConnectorContext, request: InvocationRequest | None = None
    ) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        for key, value in (context.config.get("headers") or {}).items():
            headers[key] = (
                await self._secrets.resolve(value) if is_secret_reference(value) else value
            )
        idem_header = context.config.get("idempotency_header")
        if idem_header and request:
            headers[idem_header] = request.idempotency_key
        if request:
            headers["X-FAVL-Invocation-ID"] = request.invocation_id
            if request.trace_id:
                headers["X-FAVL-Trace-ID"] = request.trace_id
        return headers

    async def health_check(self, context: ConnectorContext) -> HealthResult:
        started = datetime.now(timezone.utc)
        url = context.config.get("health_url") or context.config.get("base_url")
        if not url:
            return HealthResult(False, "no health_url or base_url configured", started)
        try:
            headers = await self._headers(context)
            async with httpx.AsyncClient() as client:
                response = await outbound.request(
                    client,
                    context.config.get("health_method", "GET"),
                    url,
                    self._policy(context.config),
                    headers=headers,
                    connect_timeout=3.0,
                    read_timeout=5.0,
                    total_timeout=8.0,
                )
        except SecretNotFound as exc:
            return HealthResult(False, f"secret unresolved: {exc.reference}", started)
        except Exception as exc:
            return HealthResult(False, f"{type(exc).__name__}: {exc}"[:200], started)

        latency = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        return HealthResult(
            healthy=200 <= response.status_code < 400,
            detail=f"HTTP {response.status_code}",
            checked_at=started,
            latency_ms=latency,
        )

    async def invoke(
        self, request: InvocationRequest, context: ConnectorContext
    ) -> InvocationResult:
        started = datetime.now(timezone.utc)
        remaining = request.seconds_remaining(started)
        if remaining <= 0:
            return InvocationResult.failure(
                ErrorCode.DEADLINE_EXCEEDED,
                "deadline passed before dispatch",
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )

        base_url = str(context.config.get("base_url", "")).rstrip("/")
        path = str(request.operation or context.config.get("path", "")).lstrip("/")
        url = f"{base_url}/{path}" if path else base_url

        try:
            headers = await self._headers(context, request)
        except SecretNotFound as exc:
            return InvocationResult.failure(
                ErrorCode.SECRET_NOT_FOUND,
                exc.reference,
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )

        try:
            async with httpx.AsyncClient() as client:
                response = await outbound.request(
                    client,
                    context.config.get("method", "POST"),
                    url,
                    self._policy(context.config),
                    json_body=request.payload,
                    headers=headers,
                    connect_timeout=min(
                        float(context.config.get("connect_timeout", 5.0)), remaining
                    ),
                    read_timeout=min(
                        float(context.config.get("read_timeout", 10.0)), remaining
                    ),
                    # The deadline always wins over per-phase timeouts.
                    total_timeout=remaining,
                )
        except httpx.TimeoutException as exc:
            return InvocationResult.failure(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"{type(exc).__name__}",
                started_at=started,
                completed_at=datetime.now(timezone.utc),
                audit_metadata={"timeout_budget_seconds": round(remaining, 3)},
            )
        except outbound.ResponseTooLarge as exc:
            return InvocationResult.failure(
                ErrorCode.RESPONSE_TOO_LARGE,
                str(exc),
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except outbound.ContentTypeRejected as exc:
            return InvocationResult.failure(
                ErrorCode.CONTENT_TYPE_REJECTED,
                str(exc),
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except outbound.TooManyRedirects as exc:
            return InvocationResult.failure(
                ErrorCode.TOO_MANY_REDIRECTS,
                str(exc),
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except SSRFBlocked as exc:
            logger.warning(
                "adapter.ssrf_blocked connector_id=%s reason=%s",
                context.connector_id,
                exc.reason,
            )
            return InvocationResult.failure(
                ErrorCode.SSRF_BLOCKED,
                exc.reason,
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )
        except httpx.HTTPError as exc:
            return InvocationResult.failure(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"{type(exc).__name__}: {exc}"[:300],
                started_at=started,
                completed_at=datetime.now(timezone.utc),
            )

        completed = datetime.now(timezone.utc)
        if 200 <= response.status_code < 300:
            try:
                parsed = json.loads(response.body) if response.body else {}
            except ValueError:
                parsed = {"raw": response.body.decode("utf-8", "replace")[:4000]}
            return InvocationResult(
                status=InvocationStatus.SUCCEEDED,
                output=parsed if isinstance(parsed, dict) else {"result": parsed},
                provider_request_id=response.provider_request_id,
                started_at=started,
                completed_at=completed,
                audit_metadata={
                    "http_status": response.status_code,
                    "redirects": response.redirects,
                    "response_bytes": len(response.body),
                },
            )

        if response.status_code in (408, 429, 502, 503, 504):
            code = ErrorCode.UPSTREAM_UNAVAILABLE
        elif 400 <= response.status_code < 500:
            code = ErrorCode.UPSTREAM_CLIENT_ERROR
        else:
            code = ErrorCode.UPSTREAM_ERROR

        return InvocationResult.failure(
            code,
            f"HTTP {response.status_code}",
            started_at=started,
            completed_at=completed,
            provider_request_id=response.provider_request_id,
            audit_metadata={"http_status": response.status_code},
        )
