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

Two rules make the classification trustworthy:

1. Addresses are NORMALISED before they are classified. An IPv4-mapped IPv6
   address such as ``::ffff:169.254.169.254`` reaches the same host as
   ``169.254.169.254``, but on Python 3.11 its ``is_link_local`` is False —
   the stdlib only began unwrapping v4-mapped forms in 3.12.4. Classifying
   the raw form would make the guard's correctness depend on the interpreter
   version, and these services run 3.11 while the test venv runs 3.13. Every
   embedded-IPv4 form is unwrapped first.

2. Forbidden ranges are declared EXPLICITLY as networks rather than inferred
   from ``is_private`` / ``is_link_local``. Those properties have changed
   between Python releases; a hard-coded table cannot.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from urllib.parse import urlparse

DEFAULT_ALLOWED_SCHEMES = ("https",)

# Cloud metadata services are the highest-value SSRF target: they hand out
# instance credentials to anything that can make a plain HTTP request from
# inside the network. Declared as networks so no string spelling slips by.
METADATA_NETWORKS: tuple[IPv4Network | IPv6Network, ...] = (
    IPv4Network("169.254.169.254/32"),  # AWS / Azure / GCP / DigitalOcean IMDS
    IPv4Network("169.254.170.2/32"),  # ECS task metadata
    IPv4Network("100.100.100.200/32"),  # Alibaba Cloud
    IPv6Network("fd00:ec2::254/128"),  # AWS IMDSv6
)

# Blocked even when a deployment explicitly permits private addresses.
ALWAYS_FORBIDDEN: tuple[tuple[IPv4Network | IPv6Network, str], ...] = (
    (IPv4Network("127.0.0.0/8"), "loopback_address"),
    (IPv6Network("::1/128"), "loopback_address"),
    (IPv4Network("169.254.0.0/16"), "link_local_address"),
    (IPv6Network("fe80::/10"), "link_local_address"),
    (IPv4Network("224.0.0.0/4"), "multicast_address"),
    (IPv6Network("ff00::/8"), "multicast_address"),
    (IPv4Network("0.0.0.0/8"), "unspecified_address"),
    (IPv6Network("::/128"), "unspecified_address"),
    # Carrier-grade NAT: routable inside many provider networks and a common
    # path to infrastructure the deployment does not own.
    (IPv4Network("100.64.0.0/10"), "shared_address_space"),
)

# Blocked unless the deployment explicitly permits private addressing.
PRIVATE_NETWORKS: tuple[tuple[IPv4Network | IPv6Network, str], ...] = (
    (IPv4Network("10.0.0.0/8"), "private_address"),
    (IPv4Network("172.16.0.0/12"), "private_address"),
    (IPv4Network("192.168.0.0/16"), "private_address"),
    (IPv6Network("fc00::/7"), "private_address"),
    (IPv4Network("192.0.0.0/24"), "reserved_address"),
    (IPv4Network("192.0.2.0/24"), "reserved_address"),
    (IPv4Network("198.18.0.0/15"), "reserved_address"),
    (IPv4Network("198.51.100.0/24"), "reserved_address"),
    (IPv4Network("203.0.113.0/24"), "reserved_address"),
    (IPv4Network("240.0.0.0/4"), "reserved_address"),
    (IPv4Network("255.255.255.255/32"), "broadcast_address"),
)

# Prefixes that embed an IPv4 address inside an IPv6 one. Each must be
# unwrapped before classification or the v4 rules above never apply.
NAT64_PREFIX = IPv6Network("64:ff9b::/96")


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
    # Operator-controlled only — see app/security/policy.py. A connector's own
    # configuration must never set this, because that would let the author of
    # a destination authorise reaching it.
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
        for raw_entry in self.allowed_hosts:
            entry = raw_entry.lower().rstrip(".")
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


def normalise_address(ip: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    """Unwrap any IPv6 form that embeds an IPv4 address.

    ``::ffff:127.0.0.1`` and ``127.0.0.1`` reach the same socket, so they must
    classify identically. Leaving this to the stdlib would tie the guard's
    behaviour to the interpreter version.
    """
    if not isinstance(ip, IPv6Address):
        return ip

    mapped = ip.ipv4_mapped
    if mapped is not None:
        return mapped

    sixtofour = ip.sixtofour
    if sixtofour is not None:
        return sixtofour

    teredo = ip.teredo
    if teredo is not None:
        # (server, client) — the client address is the reachable endpoint.
        return teredo[1]

    if ip in NAT64_PREFIX:
        return IPv4Address(int(ip) & 0xFFFFFFFF)

    return ip


def _match(
    ip: IPv4Address | IPv6Address,
    table: tuple[tuple[IPv4Network | IPv6Network, str], ...],
) -> str | None:
    for network, reason in table:
        if ip.version == network.version and ip in network:
            return reason
    return None


def _always_forbidden(ip: IPv4Address | IPv6Address) -> str | None:
    """Blocked even when private addresses are explicitly permitted.

    `allow_private_addresses` means "this deployment may reach our internal
    network". It must not also mean "may reach the service's own loopback
    interface", which is where admin endpoints, debug servers and sidecars
    listen — a materially more dangerous target than a peer service.
    """
    normalised = normalise_address(ip)
    for network in METADATA_NETWORKS:
        if normalised.version == network.version and normalised in network:
            return "cloud_metadata_endpoint"
    return _match(normalised, ALWAYS_FORBIDDEN)


def _address_is_forbidden(ip: IPv4Address | IPv6Address) -> str | None:
    normalised = normalise_address(ip)
    always = _always_forbidden(normalised)
    if always:
        return always
    return _match(normalised, PRIVATE_NETWORKS)


def classify(ip: IPv4Address | IPv6Address, allow_private: bool) -> str | None:
    """Single classification entry point used by both code paths."""
    return _always_forbidden(ip) if allow_private else _address_is_forbidden(ip)


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

    addresses: list[str] = []
    for info in infos:
        raw = str(info[4][0])
        try:
            parsed = ipaddress.ip_address(raw)
        except ValueError:
            raise SSRFBlocked("unparsable_address", raw) from None

        ip = normalise_address(parsed)
        forbidden = classify(ip, policy.allow_private_addresses)
        if forbidden:
            raise SSRFBlocked(forbidden, f"{host} -> {raw}")
        # Connect to the normalised form so the socket goes where the check
        # was made, not to the wrapper form that skipped it.
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

    # A literal address in the URL skips DNS entirely, so classify it here as
    # well as in resolve_and_validate.
    try:
        literal_ip: IPv4Address | IPv6Address | None = ipaddress.ip_address(
            host.strip("[]")
        )
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        forbidden = classify(literal_ip, policy.allow_private_addresses)
        if forbidden:
            raise SSRFBlocked(forbidden, host)

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
