"""Outbound request guard.

Without this the HTTP adapter is an SSRF primitive: any caller who can create
a connector could make the gateway fetch cloud metadata endpoints, reach
services on the internal network, or scan private address space using the
gateway's network position and credentials.

The guard resolves the hostname, validates every address it resolves to, and
then pins the connection to a validated address. Validating the name and
letting the HTTP client resolve it again separately would leave a DNS
rebinding window: the second lookup can return an address the first never
saw.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

DEFAULT_ALLOWED_SCHEMES = ("https",)

# Blocked regardless of allowlist. Cloud metadata services are the highest
# value SSRF target: they hand out instance credentials to anything that can
# make a plain HTTP request from inside the network.
METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / GCP / DigitalOcean IMDS
        "169.254.170.2",  # ECS task metadata
        "100.100.100.200",  # Alibaba Cloud
        "fd00:ec2::254",  # AWS IMDSv6
    }
)


class SSRFBlocked(Exception):
    """Raised when a destination fails validation."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class OutboundPolicy:
    allowed_hosts: frozenset[str] = frozenset()
    allowed_schemes: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMES
    # Only ever enabled deliberately, for a trusted internal destination.
    allow_private_addresses: bool = False
    max_redirects: int = 3
    max_response_bytes: int = 1_048_576
    allowed_content_types: tuple[str, ...] = (
        "application/json",
        "application/problem+json",
        "text/plain",
    )

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for entry in self.allowed_hosts:
            entry = entry.lower().rstrip(".")
            if host == entry:
                return True
            # A leading dot means "this domain and its subdomains".
            if entry.startswith(".") and host.endswith(entry):
                return True
        return False


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    host: str
    port: int
    scheme: str
    # The address the connection is pinned to, so the name is resolved once.
    address: str
    pinned_url: str = field(default="")


def _always_forbidden(ip: ipaddress._BaseAddress) -> str | None:
    """Blocked even when private addresses are explicitly permitted.

    `allow_private_addresses` means "this connector may reach our internal
    network". It must not also mean "this connector may reach the service's
    own loopback interface", which is where admin endpoints, debug servers
    and sidecars listen — a materially more dangerous target than a peer
    service on the LAN.
    """
    if str(ip) in METADATA_ADDRESSES:
        return "cloud_metadata_endpoint"
    if ip.is_loopback:
        return "loopback_address"
    if ip.is_link_local:
        return "link_local_address"
    if ip.is_multicast:
        return "multicast_address"
    if ip.is_unspecified:
        return "unspecified_address"
    return None


def _address_is_forbidden(ip: ipaddress._BaseAddress) -> str | None:
    always = _always_forbidden(ip)
    if always:
        return always
    if ip.is_reserved:
        return "reserved_address"
    if getattr(ip, "is_site_local", False):
        return "site_local_address"
    if ip.is_private:
        return "private_address"
    return None


def resolve_and_validate(host: str, port: int, policy: OutboundPolicy) -> str:
    """Resolve `host` and return one address that passed every check.

    Every resolved address must pass. A name that resolves to one public and
    one private address is rejected outright rather than quietly using the
    public one — that pattern is how rebinding attacks are staged.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFBlocked("dns_resolution_failed", f"{host}: {exc}") from None

    if not infos:
        raise SSRFBlocked("dns_resolution_failed", host)

    addresses = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise SSRFBlocked("unparsable_address", raw) from None

        forbidden = (
            _address_is_forbidden(ip)
            if not policy.allow_private_addresses
            # Loopback, link-local, multicast and metadata stay blocked even
            # for an explicitly private-allowed target.
            else _always_forbidden(ip)
        )
        if forbidden:
            raise SSRFBlocked(forbidden, f"{host} -> {raw}")
        addresses.append(str(ip))

    return addresses[0]


def validate_static(url: str, policy: OutboundPolicy) -> tuple[str, str, int]:
    """Scheme, host and credential checks with no name resolution.

    Used at configuration time. Resolution is deliberately excluded: whether
    a connector's configuration is well-formed must not depend on DNS being
    reachable, and an address checked now says nothing about the address that
    will be used at invocation time.
    """
    parsed = urlparse(url)

    scheme = (parsed.scheme or "").lower()
    if scheme not in policy.allowed_schemes:
        raise SSRFBlocked("scheme_not_allowed", scheme or "<none>")

    host = parsed.hostname
    if not host:
        raise SSRFBlocked("missing_host", url)

    if parsed.username or parsed.password:
        # Credentials in the URL would be sent to whatever the name resolves
        # to, and would land in logs.
        raise SSRFBlocked("credentials_in_url", host)

    if not policy.host_allowed(host):
        raise SSRFBlocked("host_not_allowlisted", host)

    return scheme, host, parsed.port or (443 if scheme == "https" else 80)


def validate_url(url: str, policy: OutboundPolicy) -> ValidatedTarget:
    """Full check, including resolution and address pinning. Request time."""
    parsed = urlparse(url)
    scheme, host, port = validate_static(url, policy)
    address = resolve_and_validate(host, port, policy)

    # Pin the connection to the validated address. The Host header and SNI
    # still carry the original name, so TLS verification and virtual hosting
    # keep working.
    literal = f"[{address}]" if ":" in address else address
    pinned = parsed._replace(netloc=f"{literal}:{port}").geturl()

    return ValidatedTarget(
        url=url,
        host=host,
        port=port,
        scheme=scheme,
        address=address,
        pinned_url=pinned,
    )
