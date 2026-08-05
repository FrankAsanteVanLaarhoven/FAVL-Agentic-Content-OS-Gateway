"""Adapter contract, registry dispatch, signing and secret hygiene."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "connector-registry"))

from app.adapters.base import (  # noqa: E402
    RETRYABLE_CODES,
    ConnectorAdapter,
    ConnectorContext,
    ErrorCode,
    IdempotencyMode,
    InvocationRequest,
    InvocationResult,
    InvocationStatus,
)
from app.adapters.internal import InternalAdapter  # noqa: E402
from app.adapters.registry import (  # noqa: E402
    AdapterRegistry,
    UnknownAdapterKind,
    build_registry,
)
from app.adapters.webhook import build_signature, verify_signature  # noqa: E402
from app.security.outbound import (  # noqa: E402
    STRIPPED_REQUEST_HEADERS,
    sanitise_headers,
)
from app.security.secrets import (  # noqa: E402
    EnvSecretResolver,
    SecretNotFound,
    is_secret_reference,
    redact,
)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------ #
# registry dispatch
# ------------------------------------------------------------------ #


def test_registry_registers_exactly_the_three_kinds_in_scope():
    """MCP and A2A are explicitly out of scope for M1.3."""
    assert build_registry().kinds == ["http", "internal", "webhook"]


def test_unknown_kind_raises_rather_than_falling_back():
    """A silent echo fallback is the worst failure mode for a connector."""
    with pytest.raises(UnknownAdapterKind) as exc:
        build_registry().get("mcp")
    assert exc.value.kind == "mcp"
    assert "http" in exc.value.known


def test_duplicate_registration_is_rejected():
    registry = build_registry()
    with pytest.raises(ValueError):
        registry.register(InternalAdapter())


def test_adapter_without_a_kind_is_rejected():
    class Nameless:
        kind = ""

    with pytest.raises(ValueError):
        AdapterRegistry().register(Nameless())


def test_every_registered_adapter_satisfies_the_protocol():
    registry = build_registry()
    for kind in registry.kinds:
        assert isinstance(registry.get(kind), ConnectorAdapter)


# ------------------------------------------------------------------ #
# internal adapter
# ------------------------------------------------------------------ #


def test_internal_adapter_requires_a_registered_service():
    adapter = InternalAdapter(registry={"document-parser": "http://parser:8080"})
    assert run(
        adapter.validate_config({"service": "document-parser", "operation": "x"})
    ).valid
    report = run(adapter.validate_config({"service": "unknown", "operation": "x"}))
    assert not report.valid
    assert "not registered" in report.errors[0]


def test_internal_adapter_rejects_a_caller_supplied_url():
    """Addressing by URL would make the internal adapter an SSRF primitive."""
    adapter = InternalAdapter(registry={"svc": "http://svc:8080"})
    report = run(
        adapter.validate_config(
            {"service": "svc", "operation": "x", "url": "http://169.254.169.254/"}
        )
    )
    assert not report.valid
    assert any("url is not permitted" in e for e in report.errors)


# ------------------------------------------------------------------ #
# webhook signing
# ------------------------------------------------------------------ #


def test_signature_round_trips():
    body = json.dumps({"a": 1}).encode()
    sig = build_signature("secret", "evt-1", "1754280000", body)
    assert sig.startswith("v1=")
    assert verify_signature("secret", "evt-1", "1754280000", body, sig)


@pytest.mark.parametrize(
    "field,changed",
    [
        ("secret", {"secret": "other"}),
        ("event_id", {"event_id": "evt-2"}),
        ("timestamp", {"timestamp": "1754280999"}),
        ("body", {"body": b'{"a":2}'}),
    ],
)
def test_signature_fails_when_any_signed_component_changes(field, changed):
    base = {
        "secret": "secret",
        "event_id": "evt-1",
        "timestamp": "1754280000",
        "body": b'{"a":1}',
    }
    sig = build_signature(**base)
    tampered = {**base, **changed}
    assert not verify_signature(
        tampered["secret"],
        tampered["event_id"],
        tampered["timestamp"],
        tampered["body"],
        sig,
    )


def test_timestamp_is_signed_so_replay_can_be_detected():
    """Signing the body alone would let a captured delivery replay forever."""
    body = b"{}"
    assert build_signature("s", "e", "1000", body) != build_signature(
        "s", "e", "2000", body
    )


def test_webhook_config_requires_a_secret_reference():
    adapter = build_registry().get("webhook")
    report = run(
        adapter.validate_config(
            {
                "target_url": "https://hooks.example.com/x",
                "allowed_hosts": ["hooks.example.com"],
                "signing_secret_ref": "literal-secret-value",
            }
        )
    )
    assert not report.valid
    assert any("must be a reference" in e for e in report.errors)


def test_webhook_config_rejects_an_inline_secret():
    adapter = build_registry().get("webhook")
    report = run(
        adapter.validate_config(
            {
                "target_url": "https://hooks.example.com/x",
                "allowed_hosts": ["hooks.example.com"],
                "signing_secret_ref": "env:KEY",
                "signing_secret": "hunter2",
            }
        )
    )
    assert not report.valid


# ------------------------------------------------------------------ #
# http adapter configuration
# ------------------------------------------------------------------ #


def test_http_adapter_requires_an_allowlist():
    adapter = build_registry().get("http")
    report = run(adapter.validate_config({"base_url": "https://api.example.com"}))
    assert not report.valid
    assert any("allowed_hosts is required" in e for e in report.errors)


@pytest.mark.parametrize(
    "key,value",
    [
        ("allow_private_addresses", True),
        ("allowed_schemes", ["http"]),
        ("allow_plaintext_acknowledged", True),
    ],
)
def test_connector_config_cannot_widen_outbound_reach(key, value):
    """The author of a destination must not be able to authorise reaching it.

    These keys previously came from the connector record, so any principal
    who could create a connector could point a hostname at RFC1918 space,
    set allow_private_addresses, and turn the gateway into an authenticated
    proxy into the internal network.
    """
    adapter = build_registry().get("http")
    report = run(
        adapter.validate_config(
            {
                "base_url": "https://api.example.com",
                "allowed_hosts": ["api.example.com"],
                key: value,
            }
        )
    )
    assert not report.valid
    assert any("operator-controlled" in e for e in report.errors)


def test_operator_policy_ignores_connector_supplied_private_flag(monkeypatch):
    """Even if the key reached the policy builder, it must have no effect."""
    from app.security.policy import build_policy

    monkeypatch.delenv("OUTBOUND_ALLOW_PRIVATE_ADDRESSES", raising=False)
    policy = build_policy(
        {"allowed_hosts": ["api.example.com"], "allow_private_addresses": True}
    )
    assert policy.allow_private_addresses is False


def test_operator_env_is_the_only_way_to_permit_private(monkeypatch):
    from app.security.policy import build_policy

    monkeypatch.setenv("OUTBOUND_ALLOW_PRIVATE_ADDRESSES", "true")
    assert build_policy({"allowed_hosts": ["svc"]}).allow_private_addresses is True


def test_operator_host_allowlist_is_an_upper_bound(monkeypatch):
    """A connector may choose from what the operator permits, never add."""
    from app.security.policy import build_policy

    monkeypatch.setenv("OUTBOUND_HOST_ALLOWLIST", "api.example.com")
    policy = build_policy({"allowed_hosts": ["api.example.com", "evil.example.net"]})
    assert policy.allowed_hosts == frozenset({"api.example.com"})


def test_http_adapter_rejects_a_literal_auth_header():
    adapter = build_registry().get("http")
    report = run(
        adapter.validate_config(
            {
                "base_url": "https://api.example.com",
                "allowed_hosts": ["api.example.com"],
                "headers": {"Authorization": "Bearer abc123"},
            }
        )
    )
    assert not report.valid
    assert any("secret reference" in e for e in report.errors)


def test_idempotency_mode_reflects_provider_support():
    """The gateway must not claim a guarantee the provider cannot honour."""
    adapter = build_registry().get("http")
    without = run(
        adapter.validate_config(
            {
                "base_url": "https://api.example.com",
                "allowed_hosts": ["api.example.com"],
            }
        )
    )
    with_header = run(
        adapter.validate_config(
            {
                "base_url": "https://api.example.com",
                "allowed_hosts": ["api.example.com"],
                "idempotency_header": "Idempotency-Key",
            }
        )
    )
    assert without.idempotency_mode is IdempotencyMode.GATEWAY_DEDUP_ONLY
    assert with_header.idempotency_mode is IdempotencyMode.PROVIDER_KEY


# ------------------------------------------------------------------ #
# header and secret hygiene
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "header", ["Authorization", "Cookie", "X-API-Key", "Proxy-Authorization", "Host"]
)
def test_inbound_credentials_are_never_forwarded(header):
    assert header not in sanitise_headers(
        {header: "secret", "Accept": "application/json"}
    )


def test_sanitiser_keeps_ordinary_headers():
    assert sanitise_headers({"Accept": "application/json"}) == {
        "Accept": "application/json"
    }


def test_hop_by_hop_headers_are_stripped():
    for header in ("connection", "transfer-encoding", "upgrade", "te", "trailer"):
        assert header in STRIPPED_REQUEST_HEADERS


def test_secret_references_are_recognised():
    assert is_secret_reference("env:KEY")
    assert is_secret_reference("vault:path/key")
    assert not is_secret_reference("literal")


def test_env_resolver_reads_only_addressable_references(monkeypatch):
    monkeypatch.setenv("CONNECTOR_SECRET_MY_KEY", "value")
    assert run(EnvSecretResolver().resolve("env:CONNECTOR_SECRET_MY_KEY")) == "value"
    with pytest.raises(SecretNotFound):
        run(EnvSecretResolver().resolve("vault:nope"))
    with pytest.raises(SecretNotFound):
        run(EnvSecretResolver().resolve("env:CONNECTOR_SECRET_ABSENT"))


@pytest.mark.parametrize(
    "reference",
    [
        "env:INTERNAL_SERVICE_TOKEN",
        "env:POSTGRES_PASSWORD",
        "env:KEYCLOAK_CLIENT_SECRET",
        "env:PATH",
    ],
)
def test_connector_cannot_address_operator_secrets(reference, monkeypatch):
    """A connector must not be able to name an arbitrary environment value.

    The process environment holds the database password and the token that
    authenticates /internal. An unconstrained `env:` reference in a header
    would send it to a caller-chosen host on the first invocation, which
    escalates straight back into the internal surface.
    """
    from app.security.secrets import SecretNotPermitted, is_addressable

    monkeypatch.setenv(reference.removeprefix("env:"), "operator-secret")
    assert not is_addressable(reference)
    with pytest.raises(SecretNotPermitted):
        run(EnvSecretResolver().resolve(reference))


def test_http_adapter_rejects_a_non_addressable_secret_reference():
    adapter = build_registry().get("http")
    report = run(
        adapter.validate_config(
            {
                "base_url": "https://api.example.com",
                "allowed_hosts": ["api.example.com"],
                "headers": {"X-Telemetry": "env:INTERNAL_SERVICE_TOKEN"},
            }
        )
    )
    assert not report.valid
    assert any("may not address" in e for e in report.errors)


@pytest.mark.parametrize(
    "field,requested,ceiling_env,ceiling_value",
    [
        ("max_response_bytes", 10_000_000_000, "OUTBOUND_MAX_RESPONSE_BYTES", "65536"),
        ("max_redirects", 100_000, "OUTBOUND_MAX_REDIRECTS", "3"),
    ],
)
def test_numeric_bounds_are_clamped_to_the_operator_ceiling(
    field, requested, ceiling_env, ceiling_value, monkeypatch
):
    """Caller-supplied bounds may narrow, never widen.

    These three fields stayed caller-controlled after the first fix because
    the design was a per-key denylist and they were never added to it. The
    clamp is the boundary; the denylist only produces a better message.
    """
    from app.security.policy import build_policy

    monkeypatch.setenv(ceiling_env, ceiling_value)
    policy = build_policy({"allowed_hosts": ["api.example.com"], field: requested})
    assert getattr(policy, field) == int(ceiling_value)


def test_content_types_are_intersected_not_replaced(monkeypatch):
    from app.security.policy import build_policy

    monkeypatch.delenv("OUTBOUND_ALLOWED_CONTENT_TYPES", raising=False)
    policy = build_policy(
        {"allowed_hosts": ["a"], "allowed_content_types": ["*/*", "application/json"]}
    )
    assert "*/*" not in policy.allowed_content_types
    assert "application/json" in policy.allowed_content_types


def test_narrowing_a_bound_is_honoured(monkeypatch):
    from app.security.policy import build_policy

    monkeypatch.setenv("OUTBOUND_MAX_RESPONSE_BYTES", "1048576")
    policy = build_policy({"allowed_hosts": ["a"], "max_response_bytes": 1024})
    assert policy.max_response_bytes == 1024


@pytest.mark.parametrize(
    "operation",
    ["../../admin/shutdown", "a/../../b", "http://evil.example.net/", "..%2f..%2fx"],
)
def test_internal_operation_cannot_escape_the_registered_prefix(operation):
    """`operation` is joined onto the registry base URL and is caller config."""
    adapter = InternalAdapter(registry={"svc": "http://svc:8080/api/v1"})
    report = run(adapter.validate_config({"service": "svc", "operation": operation}))
    assert not report.valid


def test_secret_not_found_names_the_reference_not_the_value():
    exc = SecretNotFound("env:MY_KEY")
    assert "env:MY_KEY" in str(exc)


def test_redaction_never_returns_the_value():
    assert redact("supersecret") == "***"
    assert "supersecret" not in redact("supersecret", keep=4)


# ------------------------------------------------------------------ #
# error classification
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.UPSTREAM_ERROR,
    ],
)
def test_transient_failures_are_retryable(code):
    assert InvocationResult.failure(code).retryable


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.UPSTREAM_CLIENT_ERROR,
        ErrorCode.SSRF_BLOCKED,
        ErrorCode.CONFIG_INVALID,
        ErrorCode.RESPONSE_TOO_LARGE,
        ErrorCode.SECRET_NOT_FOUND,
        ErrorCode.UNKNOWN_KIND,
    ],
)
def test_terminal_failures_are_not_retryable(code):
    """Retrying a 400 or a blocked host burns quota and hides the cause."""
    result = InvocationResult.failure(code)
    assert not result.retryable
    assert result.status is InvocationStatus.FAILED_TERMINAL
    assert code not in RETRYABLE_CODES


def test_timeout_maps_to_its_own_terminal_status():
    assert (
        InvocationResult.failure(ErrorCode.UPSTREAM_TIMEOUT).status
        is InvocationStatus.TIMED_OUT
    )


def test_error_detail_is_bounded():
    assert (
        len(InvocationResult.failure(ErrorCode.UPSTREAM_ERROR, "x" * 5000).error_detail)
        <= 1000
    )


# ------------------------------------------------------------------ #
# deadlines
# ------------------------------------------------------------------ #


def _request(deadline_offset: float) -> InvocationRequest:
    now = datetime.now(UTC)
    return InvocationRequest(
        invocation_id="i",
        connector_id="c",
        operation="op",
        payload={},
        idempotency_key="k" * 8,
        actor_id="a",
        tenant_id="t",
        deadline_at=now + timedelta(seconds=deadline_offset),
    )


def test_remaining_budget_shrinks_towards_the_deadline():
    assert 0 < _request(5).seconds_remaining(datetime.now(UTC)) <= 5


def test_expired_deadline_yields_no_budget():
    assert _request(-1).seconds_remaining(datetime.now(UTC)) == 0.0


def test_context_carries_references_not_secret_values():
    context = ConnectorContext(
        connector_id="c",
        connector_version=1,
        kind="webhook",
        config={"signing_secret_ref": "env:KEY"},
    )
    assert "env:KEY" in json.dumps(context.config)
    assert "hunter2" not in json.dumps(context.config)
