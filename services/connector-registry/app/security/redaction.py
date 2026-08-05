"""Redaction of connector configuration on every outbound path.

Configuration is caller-supplied and persisted. Even though the API rejects
the obvious literal-secret keys, a caller can still put a credential in a
header value or an unrecognised key. Anything that leaves the service —
API responses, outbox events, logs, span attributes — passes through here, so
a secret that slipped past validation is never republished.

Secret *references* (`env:NAME`) are shown verbatim: the reference is not
sensitive, and hiding it would make configuration impossible to audit.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# Key names that plausibly carry a credential.
SENSITIVE_KEY = re.compile(
    r"(secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|"
    r"authorization|auth|bearer|signature|salt|session)",
    re.IGNORECASE,
)

# Header names whose value is a credential by convention.
SENSITIVE_HEADER = re.compile(
    r"^(authorization|proxy-authorization|cookie|x-api-key|x-auth-token|"
    r"x-access-token|x-secret)$",
    re.IGNORECASE,
)

# A reference is safe to publish; a value never is.
REFERENCE_PREFIXES = ("secret://",)


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIXES)


# Keys that are structural rather than sensitive. Everything else that is a
# plain string is redacted, because a denylist of sensitive names can always
# be walked around: `{"auth": {"value": "..."}}` hides a credential under an
# innocuous leaf name, and only the PARENT is suspicious.
STRUCTURAL_KEYS = frozenset(
    {
        "base_url",
        "target_url",
        "health_url",
        "allowed_hosts",
        "allowed_schemes",
        "allowed_content_types",
        "service",
        "operation",
        "method",
        "health_method",
        "path",
        "kind",
        "max_redirects",
        "max_response_bytes",
        "connect_timeout",
        "read_timeout",
        "timeout_seconds",
        "idempotency_header",
        "signing_secret_ref",
    }
)


def redact_value(key: str, value: Any, *, parent_sensitive: bool = False) -> Any:
    if _is_reference(value):
        return value
    if parent_sensitive or SENSITIVE_KEY.search(key):
        return REDACTED
    # Non-string scalars cannot carry a credential.
    if not isinstance(value, str):
        return value
    # Allowlist: an unrecognised string key is redacted rather than published.
    if key.lower() in STRUCTURAL_KEYS:
        return value
    return REDACTED


def _redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Header NAMES stay visible; literal VALUES never do.

    A denylist of known-credential header names does not work: `X-Telemetry`
    is not on any list and can carry a bearer token just as well as
    `Authorization`. Since a legitimate secret is required to be a reference
    anyway, any literal value here is either non-sensitive (and no loss to
    hide) or a credential (and must be hidden). The names remain, so the
    configuration is still auditable.
    """
    return {
        str(header): (value if _is_reference(value) else REDACTED)
        for header, value in headers.items()
    }


def _redact_any(key: str, value: Any, *, parent_sensitive: bool = False) -> Any:
    """Redact a value of any shape, carrying the key's sensitivity inward.

    Lists were previously returned untouched, so a credential nested inside
    one — `{"profiles": [{"password": "..."}]}` — was published verbatim to
    the API and to NATS. Top-level key validation does not see it either,
    because it only intersects the outermost key names.
    """
    sensitive = parent_sensitive or bool(SENSITIVE_KEY.search(key))
    if isinstance(value, dict):
        if key.lower() == "headers":
            return _redact_headers(value)
        # Sensitivity is inherited: {"auth": {"value": "..."}} must redact the
        # leaf even though "value" is an innocuous name.
        return redact_config(value, parent_sensitive=sensitive)
    if isinstance(value, list | tuple):
        return [_redact_any(key, item, parent_sensitive=sensitive) for item in value]
    return redact_value(key, value, parent_sensitive=sensitive)


def redact_config(
    config: dict[str, Any], *, parent_sensitive: bool = False
) -> dict[str, Any]:
    """Deep-redact a connector configuration for publication.

    Recurses through dicts AND lists. Anything leaving the service passes
    through here, so a credential that slipped past edge validation is never
    republished.
    """
    return {
        str(key): _redact_any(str(key), value, parent_sensitive=parent_sensitive)
        for key, value in config.items()
    }
