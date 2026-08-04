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

REFERENCE_PREFIXES = ("env:", "vault:", "gsm:", "asm:")


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIXES)


def redact_value(key: str, value: Any) -> Any:
    if _is_reference(value):
        return value
    if SENSITIVE_KEY.search(key):
        return REDACTED
    return value


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Deep-redact a connector configuration for publication."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            if key.lower() == "headers":
                out[key] = {
                    header: (
                        header_value
                        if _is_reference(header_value)
                        else (
                            REDACTED
                            if SENSITIVE_HEADER.match(header)
                            or SENSITIVE_KEY.search(header)
                            else header_value
                        )
                    )
                    for header, header_value in value.items()
                }
            else:
                out[key] = redact_config(value)
        else:
            out[key] = redact_value(key, value)
    return out
