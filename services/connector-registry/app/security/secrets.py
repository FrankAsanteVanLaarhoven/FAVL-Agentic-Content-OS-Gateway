"""Secret resolution by reference.

A connector record stores a reference such as `env:WEBHOOK_SIGNING_KEY`, never
a value. Resolution happens at the moment of use and the result is never
placed on the connector, the context, the API response, an event, a log line
or a span attribute.

The environment-backed resolver is a development stand-in. M1.7 replaces it
with a real secret manager; the interface is what matters here.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

logger = logging.getLogger(__name__)

REFERENCE_PREFIXES = ("env:", "vault:", "gsm:", "asm:")


class SecretNotFound(KeyError):
    def __init__(self, reference: str) -> None:
        # The reference is safe to log; the value never is.
        super().__init__(f"secret reference not resolvable: {reference}")
        self.reference = reference


class SecretResolver(Protocol):
    async def resolve(self, reference: str) -> str: ...


def is_secret_reference(value: str) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIXES)


# Only names carrying this prefix may be referenced by a connector. Without
# it, `env:INTERNAL_SERVICE_TOKEN` in a header value would send the operator
# credential that guards /internal straight to a caller-chosen host — the
# same trust-boundary error as reading outbound policy from connector config,
# and self-escalating, because that token is the whole authentication on the
# internal invocation surface.
CONNECTOR_SECRET_PREFIX = os.getenv("CONNECTOR_SECRET_PREFIX", "CONNECTOR_SECRET_")


class SecretNotPermitted(PermissionError):
    def __init__(self, reference: str) -> None:
        super().__init__(
            f"secret reference {reference} is not addressable by a connector; "
            f"names must begin with {CONNECTOR_SECRET_PREFIX}"
        )
        self.reference = reference


def is_addressable(reference: str) -> bool:
    """Whether a connector may reference this secret at all."""
    if not reference.startswith("env:"):
        return False
    return reference[len("env:") :].startswith(CONNECTOR_SECRET_PREFIX)


class EnvSecretResolver:
    """Development resolver.

    Resolves only `env:` references whose name carries the connector secret
    prefix. Process environment is shared with database credentials, service
    tokens and provider keys; an unprefixed lookup would expose all of them.
    """

    async def resolve(self, reference: str) -> str:
        if not reference.startswith("env:"):
            raise SecretNotFound(reference)
        if not is_addressable(reference):
            logger.error("secret.not_addressable reference=%s", reference)
            raise SecretNotPermitted(reference)
        name = reference[len("env:") :]
        try:
            return os.environ[name]
        except KeyError:
            logger.error("secret.unresolved reference=%s", reference)
            raise SecretNotFound(reference) from None


def redact(value: str, keep: int = 0) -> str:
    """For audit metadata that must show a secret was used, not which one."""
    if not value:
        return ""
    if keep <= 0:
        return "***"
    return f"{value[:keep]}***"
