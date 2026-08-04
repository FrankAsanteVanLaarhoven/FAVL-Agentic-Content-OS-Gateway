"""Caller identity, derived from the gateway-verified token.

Tenant and actor were previously read straight from `X-Tenant-ID` and
`X-Actor-ID` request headers. Those are client-supplied: any caller could
name any tenant and read another tenant's invocation history simply by
changing a header. Identity must come from something the caller cannot forge.

APISIX verifies the OIDC token and injects `X-Userinfo` — base64-encoded
claims from the identity provider. The service reads identity from there, and
the gateway strips any inbound `X-Tenant-ID` / `X-Actor-ID` so a client value
can never reach this code (see gateway/apisix.yaml, proxy-rewrite headers).

`TRUST_FORWARDED_IDENTITY=false` (the default outside the gateway path) makes
a request with no verified identity fail closed rather than silently fall back
to the default tenant.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

DEFAULT_TENANT = os.getenv("DEFAULT_TENANT_ID", "default")
# Claim carrying the tenant. Keycloak deployments commonly map an
# organisation or group claim here.
TENANT_CLAIM = os.getenv("TENANT_CLAIM", "tenant")
ALLOW_ANONYMOUS = os.getenv("ALLOW_ANONYMOUS_IDENTITY", "false").lower() in {
    "1",
    "true",
    "yes",
}


@dataclass(frozen=True)
class CallerIdentity:
    actor_id: str
    tenant_id: str
    verified: bool


def _decode_userinfo(raw: str) -> dict[str, Any]:
    """Decode APISIX's base64 X-Userinfo header."""
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.b64decode(padded)
    except (binascii.Error, ValueError):
        # Some deployments forward the JSON unencoded.
        decoded = raw.encode()
    try:
        claims = json.loads(decoded)
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def identity_from_userinfo(raw: str | None) -> CallerIdentity | None:
    if not raw:
        return None
    claims = _decode_userinfo(raw)
    subject = claims.get("sub") or claims.get("preferred_username")
    if not subject:
        return None
    tenant = claims.get(TENANT_CLAIM) or DEFAULT_TENANT
    return CallerIdentity(actor_id=str(subject), tenant_id=str(tenant), verified=True)


async def current_identity(
    x_userinfo: str | None = Header(default=None, alias="X-Userinfo"),
) -> CallerIdentity:
    """FastAPI dependency. Fails closed when identity cannot be verified."""
    identity = identity_from_userinfo(x_userinfo)
    if identity is not None:
        return identity

    if ALLOW_ANONYMOUS:
        # Development and the internal service path only; never a deployment
        # that is reachable by an untrusted client.
        return CallerIdentity(
            actor_id="anonymous", tenant_id=DEFAULT_TENANT, verified=False
        )

    logger.warning("identity.unverified_request rejected")
    raise HTTPException(
        status_code=401,
        detail={
            "error_code": "IDENTITY_UNVERIFIED",
            "message": (
                "no verified caller identity; requests must arrive through "
                "the gateway, which injects claims from the validated token"
            ),
        },
    )


async def current_tenant(
    x_userinfo: str | None = Header(default=None, alias="X-Userinfo"),
) -> str:
    return (await current_identity(x_userinfo)).tenant_id


async def current_actor(
    x_userinfo: str | None = Header(default=None, alias="X-Userinfo"),
) -> str:
    return (await current_identity(x_userinfo)).actor_id
