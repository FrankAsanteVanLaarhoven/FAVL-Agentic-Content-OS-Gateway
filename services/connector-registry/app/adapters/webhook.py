"""Signed outbound webhook adapter.

A webhook is a notification, not general HTTP invocation, so this adapter
deliberately offers no method, path or header control. Every delivery is a
signed POST of a fixed envelope.

The signature covers the timestamp as well as the body. Signing the body
alone lets an attacker who captures one delivery replay it indefinitely; with
the timestamp inside the signed string the receiver can reject anything
outside a freshness window without the signature still validating.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx

from ..security import outbound
from ..security.policy import (
    build_policy,
    clamp_timeout,
    rejected_operator_keys,
)
from ..security.secrets import (
    SecretNotFound,
    SecretNotPermitted,
    SecretResolver,
    check_addressable,
    is_secret_reference,
)
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

SIGNATURE_VERSION = "v1"
DEFAULT_MAX_RESPONSE_BYTES = int(os.getenv("WEBHOOK_MAX_RESPONSE_BYTES", "65536"))


def build_signature(secret: str, event_id: str, timestamp: str, body: bytes) -> str:
    """`v1=<hex>` over `event_id.timestamp.body`.

    The event id is inside the signed string so a receiver can reject a
    replay of a different event that happens to share a timestamp.
    """
    signed = b".".join([event_id.encode(), timestamp.encode(), body])
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(
    secret: str, event_id: str, timestamp: str, body: bytes, provided: str
) -> bool:
    """Reference verifier for receivers. Constant-time by construction."""
    expected = build_signature(secret, event_id, timestamp, body)
    return hmac.compare_digest(expected, provided)


class WebhookAdapter:
    kind = "webhook"

    def __init__(self, secret_resolver: SecretResolver) -> None:
        self._secrets = secret_resolver

    def _policy(self, config: dict[str, Any]) -> OutboundPolicy:
        # A notification endpoint that redirects is misconfigured, so the
        # redirect budget is fixed at zero regardless of configuration.
        return replace(build_policy(config), max_redirects=0)

    async def validate_config(self, config: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        if not config.get("target_url"):
            errors.append("config.target_url is required")
        if not config.get("allowed_hosts"):
            errors.append("config.allowed_hosts is required")

        secret_ref = config.get("signing_secret_ref")
        if not secret_ref:
            errors.append(
                "config.signing_secret_ref is required; deliveries are signed"
            )
        elif not is_secret_reference(secret_ref):
            errors.append(
                "config.signing_secret_ref must be a reference such as "
                "env:NAME, never a literal secret"
            )
        else:
            try:
                check_addressable(secret_ref, owner="", tenant="")
            except SecretNotPermitted as exc:
                if "may not read" not in str(exc):
                    errors.append(f"config.signing_secret_ref: {exc.why}")

        if config.get("signing_secret"):
            errors.append("config.signing_secret is not permitted; use a reference")

        escalating = rejected_operator_keys(config)
        if escalating:
            errors.append(f"config may not set operator-controlled keys: {escalating}")

        if errors:
            return ValidationResult.failed(*errors)

        # A webhook receiver may or may not honour the event id for dedup, so
        # the gateway only claims what it can enforce itself.
        return ValidationResult.ok(
            supports_idempotency=True,
            idempotency_mode=IdempotencyMode.GATEWAY_DEDUP_ONLY,
        )

    async def health_check(self, context: ConnectorContext) -> HealthResult:
        """Resolvability and policy only — no unsolicited delivery is sent."""
        started = datetime.now(UTC)
        url = context.config.get("target_url")
        if not url:
            return HealthResult(False, "no target_url configured", started)
        try:
            from ..security.ssrf import validate_static

            validate_static(url, self._policy(context.config))
        except SSRFBlocked as exc:
            return HealthResult(False, f"target rejected: {exc.reason}", started)

        try:
            await self._secrets.resolve(
                context.config["signing_secret_ref"],
                owner=context.connector_id,
                tenant=context.tenant_id,
            )
        except SecretNotFound as exc:
            return HealthResult(False, f"secret unresolved: {exc.reference}", started)

        return HealthResult(
            True, "target reachable by policy; secret resolvable", started
        )

    async def invoke(
        self, request: InvocationRequest, context: ConnectorContext
    ) -> InvocationResult:
        started = datetime.now(UTC)
        remaining = request.seconds_remaining(started)
        if remaining <= 0:
            return InvocationResult.failure(
                ErrorCode.DEADLINE_EXCEEDED,
                "deadline passed before dispatch",
                started_at=started,
                completed_at=datetime.now(UTC),
            )

        try:
            secret = await self._secrets.resolve(
                context.config["signing_secret_ref"],
                owner=context.connector_id,
                tenant=context.tenant_id,
            )
        except (SecretNotFound, KeyError) as exc:
            return InvocationResult.failure(
                ErrorCode.SECRET_NOT_FOUND,
                getattr(exc, "reference", "signing_secret_ref"),
                started_at=started,
                completed_at=datetime.now(UTC),
            )

        timestamp = str(int(started.timestamp()))
        envelope = {
            "event_id": request.invocation_id,
            "event_type": request.operation or "connector.webhook",
            "occurred_at": started.isoformat(),
            "connector_id": context.connector_id,
            "data": request.payload,
        }
        body = json.dumps(envelope, separators=(",", ":"), default=str).encode()
        signature = build_signature(secret, request.invocation_id, timestamp, body)

        headers = {
            "Content-Type": "application/json",
            "X-FAVL-Event-ID": request.invocation_id,
            "X-FAVL-Timestamp": timestamp,
            "X-FAVL-Signature": signature,
            "X-FAVL-Delivery-Attempt": str(request.attempt),
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await outbound.request(
                    client,
                    "POST",
                    context.config["target_url"],
                    self._policy(context.config),
                    content=body,
                    headers=headers,
                    connect_timeout=min(
                        clamp_timeout(context.config.get("connect_timeout"), 5.0),
                        remaining,
                    ),
                    read_timeout=min(
                        clamp_timeout(context.config.get("read_timeout"), 10.0),
                        remaining,
                    ),
                    total_timeout=remaining,
                )
        except httpx.TimeoutException:
            return InvocationResult.failure(
                ErrorCode.UPSTREAM_TIMEOUT,
                "webhook delivery timed out",
                started_at=started,
                completed_at=datetime.now(UTC),
            )
        except outbound.ResponseTooLarge as exc:
            return InvocationResult.failure(
                ErrorCode.RESPONSE_TOO_LARGE,
                str(exc),
                started_at=started,
                completed_at=datetime.now(UTC),
            )
        except SSRFBlocked as exc:
            return InvocationResult.failure(
                ErrorCode.SSRF_BLOCKED,
                exc.reason,
                started_at=started,
                completed_at=datetime.now(UTC),
            )
        except httpx.HTTPError as exc:
            return InvocationResult.failure(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"{type(exc).__name__}"[:200],
                started_at=started,
                completed_at=datetime.now(UTC),
            )

        completed = datetime.now(UTC)
        # The delivery record proves what was sent without retaining the body
        # or the signing secret.
        audit = {
            "http_status": response.status_code,
            "delivery_attempt": request.attempt,
            "signed_bytes": len(body),
            "signature_version": SIGNATURE_VERSION,
            "target_host": httpx.URL(context.config["target_url"]).host,
        }

        if 200 <= response.status_code < 300:
            return InvocationResult(
                status=InvocationStatus.SUCCEEDED,
                output={"delivered": True, "http_status": response.status_code},
                provider_request_id=response.provider_request_id,
                started_at=started,
                completed_at=completed,
                audit_metadata=audit,
            )

        code = (
            ErrorCode.UPSTREAM_UNAVAILABLE
            if response.status_code in (408, 429, 502, 503, 504)
            else (
                ErrorCode.UPSTREAM_CLIENT_ERROR
                if 400 <= response.status_code < 500
                else ErrorCode.UPSTREAM_ERROR
            )
        )
        return InvocationResult.failure(
            code,
            f"HTTP {response.status_code}",
            started_at=started,
            completed_at=completed,
            provider_request_id=response.provider_request_id,
            audit_metadata=audit,
        )
