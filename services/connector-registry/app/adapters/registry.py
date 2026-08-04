"""Adapter registry and dispatch.

Dispatch is registry-based and exhaustive. An unregistered kind fails at
registration time if possible and deterministically at invocation otherwise —
there is no echo fallback, because a silent no-op that reports success is the
worst available failure mode for a connector runtime.
"""

from __future__ import annotations

import logging
from typing import Any

from ..security.secrets import EnvSecretResolver, SecretResolver
from .base import ConnectorAdapter
from .http import HttpAdapter
from .internal import InternalAdapter
from .webhook import WebhookAdapter

logger = logging.getLogger(__name__)


class UnknownAdapterKind(LookupError):
    def __init__(self, kind: str, known: list[str]) -> None:
        super().__init__(
            f"no adapter registered for kind '{kind}'; registered kinds: {known}"
        )
        self.kind = kind
        self.known = known


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ConnectorAdapter] = {}

    def register(self, adapter: ConnectorAdapter) -> None:
        kind = getattr(adapter, "kind", None)
        if not kind:
            raise ValueError("adapter must declare a non-empty `kind`")
        if kind in self._adapters:
            raise ValueError(f"adapter kind '{kind}' is already registered")
        self._adapters[kind] = adapter
        logger.info("adapter.registered kind=%s impl=%s", kind, type(adapter).__name__)

    def get(self, kind: str) -> ConnectorAdapter:
        try:
            return self._adapters[kind]
        except KeyError:
            raise UnknownAdapterKind(kind, self.kinds) from None

    def has(self, kind: str) -> bool:
        return kind in self._adapters

    @property
    def kinds(self) -> list[str]:
        return sorted(self._adapters)


def build_registry(secret_resolver: SecretResolver | None = None) -> AdapterRegistry:
    """The three kinds in scope for M1.3. MCP and A2A are deliberately absent."""
    resolver = secret_resolver or EnvSecretResolver()
    registry = AdapterRegistry()
    registry.register(InternalAdapter())
    registry.register(HttpAdapter(resolver))
    registry.register(WebhookAdapter(resolver))
    return registry


def registry_snapshot(registry: AdapterRegistry) -> dict[str, Any]:
    return {"kinds": registry.kinds, "count": len(registry.kinds)}
