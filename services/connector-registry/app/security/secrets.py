"""Secret resolution by namespaced reference.

A connector names a LOGICAL secret; it never names a storage location. The
reference form is

    secret://connector/<connector-name>/<key>
    secret://tenant/<tenant-id>/<key>

and the resolver maps that to whatever backend the deployment uses. There is
no generic environment lookup, so there is no expression a connector can
write that reaches an arbitrary variable.

That distinction is the whole point. An earlier version accepted `env:NAME`
and resolved it straight out of `os.environ`, which meant a connector could
put `env:INTERNAL_SERVICE_TOKEN` in a header value and have the credential
guarding the internal invocation surface delivered to a host of its choosing.
Constraining the *names* — a prefix allowlist — narrowed that hole but kept
the shape of it: the connector was still addressing storage directly, and the
safety depended on every future variable being named correctly.

Under this scheme the connector cannot express a storage address at all. The
namespace is derived from the connector's own identity and tenant, so one
connector cannot reference another's secret even by guessing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

SCHEME = "secret://"

# secret://<scope>/<owner>/<key>
REFERENCE = re.compile(
    r"^secret://(?P<scope>connector|tenant)/(?P<owner>[A-Za-z0-9._-]{1,128})/"
    r"(?P<key>[A-Za-z0-9._-]{1,128})$"
)

# Legacy `env:` references are refused outright rather than translated. A
# silent translation would let an old record keep working while looking as if
# it had been migrated.
LEGACY_PREFIXES = ("env:", "vault:", "gsm:", "asm:")


@dataclass(frozen=True)
class SecretRef:
    scope: str
    owner: str
    key: str

    def as_text(self) -> str:
        return f"{SCHEME}{self.scope}/{self.owner}/{self.key}"

    def env_name(self) -> str:
        """Storage name for the development, environment-backed resolver.

        Derived from the reference; never supplied by the connector.
        """
        safe = re.sub(r"[^A-Za-z0-9]", "_", f"{self.scope}_{self.owner}_{self.key}")
        return f"FAVL_SECRET_{safe.upper()}"


class SecretNotFound(KeyError):
    def __init__(self, reference: str) -> None:
        # The reference is safe to log; the value never is.
        super().__init__(f"secret reference not resolvable: {reference}")
        self.reference = reference


class SecretNotPermitted(PermissionError):
    def __init__(self, reference: str, why: str) -> None:
        super().__init__(f"secret reference {reference} is not permitted: {why}")
        self.reference = reference
        self.why = why


class SecretResolver(Protocol):
    async def resolve(self, reference: str, *, owner: str, tenant: str) -> str: ...


def is_secret_reference(value: object) -> bool:
    """Whether a config value is intended as a secret reference of any kind.

    Legacy prefixes count, so validation can reject them with a useful
    message rather than treating them as literal values.
    """
    return isinstance(value, str) and (
        value.startswith(SCHEME) or value.startswith(LEGACY_PREFIXES)
    )


def parse_reference(value: str) -> SecretRef:
    if value.startswith(LEGACY_PREFIXES):
        raise SecretNotPermitted(
            value,
            "direct storage references are no longer accepted; use "
            "secret://connector/<name>/<key>",
        )
    match = REFERENCE.match(value)
    if not match:
        raise SecretNotPermitted(value, f"malformed reference; expected {SCHEME}…")
    return SecretRef(**match.groupdict())


def check_addressable(value: str, *, owner: str, tenant: str) -> SecretRef:
    """Parse and confirm the caller may address this secret.

    A connector may read its own secrets and its tenant's, and nothing else.
    Ownership comes from the record, never from the reference, so guessing
    another connector's name buys nothing.
    """
    ref = parse_reference(value)
    if ref.scope == "connector" and ref.owner != owner:
        raise SecretNotPermitted(
            value, f"connector '{owner}' may not read connector '{ref.owner}' secrets"
        )
    if ref.scope == "tenant" and ref.owner != tenant:
        raise SecretNotPermitted(
            value, f"tenant '{tenant}' may not read tenant '{ref.owner}' secrets"
        )
    return ref


class EnvSecretResolver:
    """Development resolver.

    The environment variable name is DERIVED from the reference, so the
    connector never names a variable. M1.7 swaps this for a secret manager;
    the interface does not change.
    """

    async def resolve(self, reference: str, *, owner: str, tenant: str) -> str:
        ref = check_addressable(reference, owner=owner, tenant=tenant)
        name = ref.env_name()
        try:
            return os.environ[name]
        except KeyError:
            logger.error(
                "secret.unresolved reference=%s derived_name=%s", reference, name
            )
            raise SecretNotFound(reference) from None


def redact(value: str, keep: int = 0) -> str:
    """For audit metadata that must show a secret was used, not which one."""
    if not value:
        return ""
    if keep <= 0:
        return "***"
    return f"{value[:keep]}***"
