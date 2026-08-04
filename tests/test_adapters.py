"""Adapter contract, registry dispatch, signing and secret hygiene."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
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
from app.security.outbound import STRIPPED_REQUEST_HEADERS, sanitise_headers  # noqa: E402
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
    assert run(adapter.validate_config({"service": "document-parser", "operation": "x"})).valid
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


def test_http_adapter_requires_acknowledgement_for_plaintext():
    adapter = build_registry().get("http")
    report = run(
        adapter.validate_config(
            {
                "base_url": "http://api.example.com",
                "allowed_hosts": ["api.example.com"],
                "allowed_schemes": ["http"],
            }
        )
    )
    assert not report.valid
    assert any("allow_plaintext_acknowledged" in e for e in report.errors)


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
            {"base_url": "https://api.example.com", "allowed_hosts": ["api.example.com"]}
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
    assert header not in sanitise_headers({header: "secret", "Accept": "application/json"})


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


def test_env_resolver_reads_only_env_references(monkeypatch):
    monkeypatch.setenv("MY_KEY", "value")
    assert run(EnvSecretResolver().resolve("env:MY_KEY")) == "value"
    with pytest.raises(SecretNotFound):
        run(EnvSecretResolver().resolve("vault:nope"))
    with pytest.raises(SecretNotFound):
        run(EnvSecretResolver().resolve("env:ABSENT"))


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
    [ErrorCode.UPSTREAM_TIMEOUT, ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.UPSTREAM_ERROR],
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
    assert len(InvocationResult.failure(ErrorCode.UPSTREAM_ERROR, "x" * 5000).error_detail) <= 1000


# ------------------------------------------------------------------ #
# deadlines
# ------------------------------------------------------------------ #


def _request(deadline_offset: float) -> InvocationRequest:
    now = datetime.now(timezone.utc)
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
    assert 0 < _request(5).seconds_remaining(datetime.now(timezone.utc)) <= 5


def test_expired_deadline_yields_no_budget():
    assert _request(-1).seconds_remaining(datetime.now(timezone.utc)) == 0.0


def test_context_carries_references_not_secret_values():
    context = ConnectorContext(
        connector_id="c",
        connector_version=1,
        kind="webhook",
        config={"signing_secret_ref": "env:KEY"},
    )
    assert "env:KEY" in json.dumps(context.config)
    assert "hunter2" not in json.dumps(context.config)
