"""SSRF controls.

These are the tests that matter most in M1.3: without them the HTTP adapter
turns the gateway's network position into an open proxy for anyone who can
create a connector.
"""

import ipaddress
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "connector-registry"))

from app.security.ssrf import (  # noqa: E402
    METADATA_ADDRESSES,
    OutboundPolicy,
    SSRFBlocked,
    _address_is_forbidden,
    validate_url,
)

ALLOWED = OutboundPolicy(allowed_hosts=frozenset({"api.example.com", ".trusted.test"}))


def _resolving_to(*addresses):
    """Patch getaddrinfo so a hostname resolves to chosen addresses."""
    infos = [(2, 1, 6, "", (addr, 443)) for addr in addresses]
    return patch("app.security.ssrf.socket.getaddrinfo", return_value=infos)


# ------------------------------------------------------------------ #
# address classification
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "address,reason",
    [
        ("127.0.0.1", "loopback_address"),
        ("127.0.0.53", "loopback_address"),
        ("::1", "loopback_address"),
        ("10.0.0.5", "private_address"),
        ("172.16.4.1", "private_address"),
        ("192.168.1.1", "private_address"),
        ("169.254.169.254", "cloud_metadata_endpoint"),
        ("169.254.170.2", "cloud_metadata_endpoint"),
        ("100.100.100.200", "cloud_metadata_endpoint"),
        ("169.254.1.1", "link_local_address"),
        ("224.0.0.1", "multicast_address"),
        ("0.0.0.0", "unspecified_address"),
        ("fc00::1", "private_address"),
    ],
)
def test_forbidden_addresses_are_classified(address, reason):
    assert _address_is_forbidden(ipaddress.ip_address(address)) == reason


@pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700::1111"])
def test_public_addresses_are_permitted(address):
    assert _address_is_forbidden(ipaddress.ip_address(address)) is None


# ------------------------------------------------------------------ #
# scheme, host and credential checks
# ------------------------------------------------------------------ #


def test_plaintext_http_is_rejected_by_default():
    with pytest.raises(SSRFBlocked) as exc:
        validate_url("http://api.example.com/x", ALLOWED)
    assert exc.value.reason == "scheme_not_allowed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/"])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(SSRFBlocked):
        validate_url(url, ALLOWED)


def test_host_must_be_allowlisted():
    with _resolving_to("93.184.216.34"):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("https://evil.example.net/x", ALLOWED)
    assert exc.value.reason == "host_not_allowlisted"


def test_suffix_entry_matches_subdomains():
    with _resolving_to("93.184.216.34"):
        target = validate_url("https://a.trusted.test/x", ALLOWED)
    assert target.host == "a.trusted.test"


def test_suffix_entry_does_not_match_a_lookalike_domain():
    """`.trusted.test` must not match `nottrusted.test`."""
    with _resolving_to("93.184.216.34"):
        with pytest.raises(SSRFBlocked):
            validate_url("https://eviltrusted.test/x", ALLOWED)


def test_credentials_in_url_are_rejected():
    with pytest.raises(SSRFBlocked) as exc:
        validate_url("https://user:pw@api.example.com/x", ALLOWED)
    assert exc.value.reason == "credentials_in_url"


# ------------------------------------------------------------------ #
# resolution
# ------------------------------------------------------------------ #


def test_allowlisted_host_resolving_to_loopback_is_rejected():
    """An allowlist entry is not a licence to reach localhost."""
    with _resolving_to("127.0.0.1"):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("https://api.example.com/x", ALLOWED)
    assert exc.value.reason == "loopback_address"


def test_allowlisted_host_resolving_to_metadata_is_rejected():
    with _resolving_to("169.254.169.254"):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("https://api.example.com/x", ALLOWED)
    assert exc.value.reason == "cloud_metadata_endpoint"


def test_mixed_resolution_is_rejected_entirely():
    """Public plus private must fail, not silently pick the public one.

    Returning both is exactly how a rebinding attack is staged.
    """
    with _resolving_to("93.184.216.34", "10.0.0.1"):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("https://api.example.com/x", ALLOWED)
    assert exc.value.reason == "private_address"


def test_connection_is_pinned_to_the_validated_address():
    """The pinned URL closes the DNS rebinding window between check and use."""
    with _resolving_to("93.184.216.34"):
        target = validate_url("https://api.example.com/path?q=1", ALLOWED)
    assert target.address == "93.184.216.34"
    assert "93.184.216.34" in target.pinned_url
    assert target.host == "api.example.com"
    assert target.pinned_url.endswith("/path?q=1")


def test_ipv6_pinning_is_bracketed():
    with _resolving_to("2606:4700::1111"):
        target = validate_url("https://api.example.com/x", ALLOWED)
    assert "[2606:4700::1111]" in target.pinned_url


@pytest.mark.parametrize(
    "address,reason",
    [
        ("127.0.0.1", "loopback_address"),
        ("::1", "loopback_address"),
        ("169.254.169.254", "cloud_metadata_endpoint"),
        ("169.254.1.1", "link_local_address"),
        ("224.0.0.1", "multicast_address"),
        ("0.0.0.0", "unspecified_address"),
    ],
)
def test_dangerous_ranges_stay_blocked_when_private_is_allowed(address, reason):
    """`allow_private_addresses` means the internal network, not localhost.

    Loopback reaches the service's own admin and debug surfaces, which is a
    materially worse target than a peer service on the LAN.
    """
    policy = OutboundPolicy(
        allowed_hosts=frozenset({"svc.internal"}),
        allowed_schemes=("http", "https"),
        allow_private_addresses=True,
    )
    with _resolving_to(address):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("http://svc.internal/x", policy)
    assert exc.value.reason == reason


def test_metadata_stays_blocked_even_when_private_is_allowed():
    """The private-address escape hatch must not open the credential door."""
    policy = OutboundPolicy(
        allowed_hosts=frozenset({"api.example.com"}), allow_private_addresses=True
    )
    with _resolving_to("169.254.169.254"):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("https://api.example.com/x", policy)
    assert exc.value.reason == "cloud_metadata_endpoint"


def test_private_addresses_reachable_only_when_explicitly_allowed():
    policy = OutboundPolicy(
        allowed_hosts=frozenset({"svc.internal"}),
        allowed_schemes=("http", "https"),
        allow_private_addresses=True,
    )
    with _resolving_to("10.1.2.3"):
        target = validate_url("http://svc.internal/op", policy)
    assert target.address == "10.1.2.3"


def test_dns_failure_is_blocked_not_ignored():
    import socket as real_socket

    with patch(
        "app.security.ssrf.socket.getaddrinfo",
        side_effect=real_socket.gaierror("nope"),
    ):
        with pytest.raises(SSRFBlocked) as exc:
            validate_url("https://api.example.com/x", ALLOWED)
    assert exc.value.reason == "dns_resolution_failed"


def test_every_known_metadata_address_is_covered():
    for address in METADATA_ADDRESSES:
        assert _address_is_forbidden(ipaddress.ip_address(address)) == (
            "cloud_metadata_endpoint"
        )
