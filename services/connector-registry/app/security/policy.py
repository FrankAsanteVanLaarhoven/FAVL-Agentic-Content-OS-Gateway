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
#
# This denylist is kept for the clear error message, but it is NOT the
# security boundary. A denylist makes every future OutboundPolicy field
# unsafe by default, and that is exactly how max_response_bytes,
# max_redirects and allowed_content_types stayed caller-controlled after the
# first fix: they were never added to the list. The boundary is the clamping
# in build_policy, which cannot be bypassed by adding a new key.
OPERATOR_ONLY_CONFIG_KEYS = frozenset(
    {
        "allow_private_addresses",
        "allowed_schemes",
        "allow_plaintext_acknowledged",
    }
)

DEFAULT_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# Ceilings, read at call time so a deployment (and a test) can change them
# without reimporting the module.
def max_response_bytes_ceiling() -> int:
    return int(os.getenv("OUTBOUND_MAX_RESPONSE_BYTES", "1048576"))


def max_redirects_ceiling() -> int:
    return int(os.getenv("OUTBOUND_MAX_REDIRECTS", "3"))


def allowed_content_types_ceiling() -> tuple[str, ...]:
    return _csv("OUTBOUND_ALLOWED_CONTENT_TYPES") or DEFAULT_CONTENT_TYPES


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

    # Every numeric field is clamped with min(), and every set field is
    # intersected. A connector asking for 10 GB or 100000 redirects gets the
    # operator ceiling, not its request. `max_redirects` is additionally
    # clamped by the caller of this function (the webhook adapter passes 0).
    bytes_ceiling = max_response_bytes_ceiling()
    redirect_ceiling = max_redirects_ceiling()
    type_ceiling = allowed_content_types_ceiling()

    requested_bytes = _positive_int(config.get("max_response_bytes"))
    requested_redirects = _positive_int(config.get("max_redirects"))
    requested_types = tuple(config.get("allowed_content_types") or ())

    return OutboundPolicy(
        allowed_hosts=hosts,
        allowed_schemes=schemes,
        allow_private_addresses=operator_allows_private_addresses(),
        max_redirects=min(
            requested_redirects if requested_redirects is not None else max_redirects,
            redirect_ceiling,
        ),
        max_response_bytes=min(
            requested_bytes if requested_bytes is not None else bytes_ceiling,
            bytes_ceiling,
        ),
        allowed_content_types=tuple(
            candidate for candidate in requested_types if candidate in type_ceiling
        )
        or type_ceiling,
    )


def _positive_int(value: Any) -> int | None:
    """Parse a caller-supplied bound, ignoring anything nonsensical."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def rejected_operator_keys(config: dict[str, Any]) -> list[str]:
    """Operator-only keys present in a caller-supplied configuration."""
    return sorted(OPERATOR_ONLY_CONFIG_KEYS.intersection(config))
