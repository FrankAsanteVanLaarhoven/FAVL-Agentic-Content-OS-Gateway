"""Operator-controlled outbound policy.

The critical rule: a connector's configuration may only NARROW the outbound
policy, never widen it. Anything that widens reach — permitting private
addressing, permitting plaintext, permitting a scheme — is read from the
deployment environment, which only an operator can change.

The earlier design read `allow_private_addresses` straight from the connector
record. Because any authenticated principal can create a connector, that let
the author of a destination authorise reaching it: point a hostname at
10.0.0.0/8, set the flag, and the gateway becomes an authenticated proxy into
the internal network. A destination must never be able to vouch for itself.
"""

from __future__ import annotations

import os
from typing import Any

from .ssrf import DEFAULT_ALLOWED_SCHEMES, OutboundPolicy

# Keys a connector may not set: each one would widen reach.
OPERATOR_ONLY_CONFIG_KEYS = frozenset(
    {
        "allow_private_addresses",
        "allowed_schemes",
        "allow_plaintext_acknowledged",
    }
)


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def operator_allows_private_addresses() -> bool:
    """Deployment-wide. False in any environment that has not opted in."""
    return _flag("OUTBOUND_ALLOW_PRIVATE_ADDRESSES", False)


def operator_allowed_schemes() -> tuple[str, ...]:
    schemes = _csv("OUTBOUND_ALLOWED_SCHEMES")
    return schemes or DEFAULT_ALLOWED_SCHEMES


def operator_host_allowlist() -> frozenset[str]:
    """Upper bound on destinations. Empty means "any public host".

    Address classification, not this list, is the SSRF control; the list is
    defence in depth for deployments that want an explicit egress boundary.
    """
    return frozenset(_csv("OUTBOUND_HOST_ALLOWLIST"))


def build_policy(config: dict[str, Any], *, max_redirects: int = 3) -> OutboundPolicy:
    """Combine operator bounds with the connector's narrowing preferences."""
    operator_hosts = operator_host_allowlist()
    connector_hosts = frozenset(config.get("allowed_hosts") or [])

    if operator_hosts:
        # Intersection: a connector may pick from what the operator permits,
        # never add to it.
        hosts = frozenset(
            host
            for host in connector_hosts
            if any(
                host.lower().rstrip(".") == entry.lower().rstrip(".")
                or (entry.startswith(".") and host.lower().endswith(entry.lower()))
                for entry in operator_hosts
            )
        )
    else:
        hosts = connector_hosts

    # A connector may request fewer schemes than the operator permits.
    requested = tuple(config.get("requested_schemes") or ())
    allowed = operator_allowed_schemes()
    schemes = tuple(s for s in requested if s in allowed) or allowed

    return OutboundPolicy(
        allowed_hosts=hosts,
        allowed_schemes=schemes,
        allow_private_addresses=operator_allows_private_addresses(),
        max_redirects=int(config.get("max_redirects", max_redirects)),
        max_response_bytes=int(
            config.get(
                "max_response_bytes",
                int(os.getenv("OUTBOUND_MAX_RESPONSE_BYTES", "1048576")),
            )
        ),
        allowed_content_types=tuple(
            config.get(
                "allowed_content_types",
                ("application/json", "application/problem+json", "text/plain"),
            )
        ),
    )


def rejected_operator_keys(config: dict[str, Any]) -> list[str]:
    """Operator-only keys present in a caller-supplied configuration."""
    return sorted(OPERATOR_ONLY_CONFIG_KEYS.intersection(config))
