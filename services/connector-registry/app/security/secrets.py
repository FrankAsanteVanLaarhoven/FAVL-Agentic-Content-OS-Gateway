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


class EnvSecretResolver:
    """Development resolver. Only `env:` references are supported."""

    async def resolve(self, reference: str) -> str:
        if not reference.startswith("env:"):
            raise SecretNotFound(reference)
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
