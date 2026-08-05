#!/usr/bin/env python3
"""Mutation testing for the invariants in docs/adr/0001-security-invariants.md.

A passing test proves the code does something. It does not prove the test
would notice if the code stopped doing it. Every defect this project found by
adversarial review was, at the moment it was introduced, covered by a green
suite — so the only evidence that a guard is guarded is watching its test go
red when the guard is removed.

Each mutation below deletes or inverts one control and asserts that a named
test fails. A mutation that SURVIVES is a blind spot: the control could be
removed in a refactor and nothing would say so.

    python3 scripts/mutate.py            # run every mutation
    python3 scripts/mutate.py --list     # show them without running
    python3 scripts/mutate.py -k tenant  # run a subset

Only mutations covered by the offline suite live here. Controls that need the
live stack (tenant isolation across services, alert firing) are mutated by
hand and recorded in the ADR, because spinning the stack per mutation would
make this unusable.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


@dataclass(frozen=True)
class Mutation:
    """One removed control, and the test that must notice."""

    name: str
    invariant: str
    file: str
    find: str
    replace: str
    expect_failing: str

    @property
    def path(self) -> Path:
        return ROOT / self.file


MUTATIONS: list[Mutation] = [
    Mutation(
        name="remove-private-address-clamp",
        invariant="I1",
        file="services/connector-registry/app/security/policy.py",
        find="        allow_private_addresses=operator_allows_private_addresses(),",
        replace="        allow_private_addresses=bool(config.get('allow_private_addresses')),",
        expect_failing="test_operator_policy_ignores_connector_supplied_private_flag",
    ),
    Mutation(
        name="remove-response-size-clamp",
        invariant="I1",
        file="services/connector-registry/app/security/policy.py",
        find="            requested_bytes if requested_bytes is not None else bytes_ceiling,\n            bytes_ceiling,",
        replace="            requested_bytes if requested_bytes is not None else bytes_ceiling,\n            requested_bytes or bytes_ceiling,",
        expect_failing="test_numeric_bounds_are_clamped_to_the_operator_ceiling",
    ),
    Mutation(
        name="remove-host-allowlist-intersection",
        invariant="I1",
        file="services/connector-registry/app/security/policy.py",
        find="    else:\n        hosts = connector_hosts",
        replace="    else:\n        hosts = connector_hosts\n    hosts = connector_hosts",
        expect_failing="test_operator_host_allowlist_is_an_upper_bound",
    ),
    Mutation(
        name="remove-embedded-ipv4-normalisation",
        invariant="I2",
        file="services/connector-registry/app/security/ssrf.py",
        find="    mapped = ip.ipv4_mapped\n    if mapped is not None:\n        return mapped",
        replace="    mapped = None\n    if mapped is not None:\n        return mapped",
        expect_failing="test_embedded_ipv4_forms_are_unwrapped_before_classification",
    ),
    Mutation(
        name="allow-loopback-when-private-permitted",
        invariant="I2",
        file="services/connector-registry/app/security/ssrf.py",
        find='    (IPv4Network("127.0.0.0/8"), "loopback_address"),',
        replace="",
        expect_failing="test_dangerous_ranges_stay_blocked_when_private_is_allowed",
    ),
    Mutation(
        name="accept-any-scheme",
        invariant="I2",
        file="services/connector-registry/app/security/ssrf.py",
        find='    if scheme not in policy.allowed_schemes:\n        raise SSRFBlocked("scheme_not_allowed", scheme or "<none>")',
        replace="    pass",
        expect_failing="test_non_http_schemes_are_rejected",
    ),
    Mutation(
        name="remove-address-pinning",
        invariant="I3",
        file="services/connector-registry/app/security/ssrf.py",
        find='    literal = f"[{address}]" if ":" in address else address\n    pinned = parsed._replace(netloc=f"{literal}:{port}").geturl()',
        replace="    pinned = url",
        expect_failing="test_connection_is_pinned_to_the_validated_address",
    ),
    Mutation(
        name="accept-one-good-address-of-many",
        invariant="I3",
        file="services/connector-registry/app/security/ssrf.py",
        find='        if forbidden:\n            raise SSRFBlocked(forbidden, f"{host} -> {raw}")',
        replace="        if forbidden:\n            continue",
        expect_failing="test_mixed_resolution_is_rejected_entirely",
    ),
    Mutation(
        name="allow-arbitrary-secret-storage",
        invariant="I6",
        file="services/connector-registry/app/security/secrets.py",
        find='    if value.startswith(LEGACY_PREFIXES):\n        raise SecretNotPermitted(\n            value,\n            "direct storage references are no longer accepted; use "\n            "secret://connector/<name>/<key>",\n        )',
        replace="    pass",
        expect_failing="test_legacy_env_references_are_refused_not_translated",
    ),
    Mutation(
        name="ignore-secret-ownership",
        invariant="I6",
        file="services/connector-registry/app/security/secrets.py",
        find='    if ref.scope == "connector" and ref.owner != owner:',
        replace='    if ref.scope == "connector" and False:',
        expect_failing="test_a_connector_cannot_read_another_connectors_secret",
    ),
    Mutation(
        name="publish-literal-header-values",
        invariant="I7",
        file="services/connector-registry/app/security/redaction.py",
        find="        str(header): (value if _is_reference(value) else REDACTED)",
        replace="        str(header): value",
        expect_failing="test_literal_header_values_are_never_published",
    ),
    Mutation(
        name="stop-inheriting-sensitivity",
        invariant="I7",
        file="services/connector-registry/app/security/redaction.py",
        find="    sensitive = parent_sensitive or bool(SENSITIVE_KEY.search(key))",
        replace="    sensitive = bool(SENSITIVE_KEY.search(key))",
        expect_failing="test_sensitivity_is_inherited_by_nested_values",
    ),
    Mutation(
        name="dead-letter-on-the-row-not-the-policy",
        invariant="I8",
        file="packages/favl-outbox/favl_outbox/publisher.py",
        find="        limit = min(self.retry_policy.max_attempts, row.max_attempts)",
        replace="        limit = row.max_attempts",
        expect_failing="test_dead_letter_limit_matches_the_gated_policy",
    ),
    Mutation(
        name="skip-the-duplicate-window-gate",
        invariant="I8",
        file="packages/favl-outbox/favl_outbox/timing.py",
        find="    if horizon > limit:",
        replace="    if False:",
        expect_failing="test_invariant_rejects_a_horizon_that_exceeds_the_window",
    ),
    Mutation(
        name="accept-any-aggregate-version",
        invariant="I8",
        file="packages/favl-outbox/favl_outbox/consumer.py",
        find="    if incoming == last_applied + 1:\n        return APPLY\n    return QUARANTINE",
        replace="    return APPLY",
        expect_failing="test_a_gap_is_quarantined_rather_than_accepted",
    ),
    Mutation(
        name="stop-classifying-malformed-envelopes",
        invariant="I8",
        file="packages/favl-outbox/favl_outbox/consumer.py",
        find='    for required in ("event_id", "event_type"):\n        if not payload.get(required):\n            SCHEMA_REJECTED.labels("malformed_envelope").inc()\n            raise MalformedEnvelope(required)',
        replace="    pass",
        expect_failing="test_malformed_v1_envelope_is_classified_not_crashed",
    ),
    Mutation(
        name="treat-legacy-events-as-v1",
        invariant="I8",
        file="packages/favl-outbox/favl_outbox/consumer.py",
        find="    if schema_version is None:\n        return parse_legacy_event(payload, subject)",
        replace="    if schema_version is None:\n        return parse_v1_event({**payload, 'event_id': 'x', 'event_type': subject or 'x'})",
        expect_failing="test_legacy_event_is_identified_as_version_zero",
    ),
]


def run_suite(selector: str) -> bool:
    """Run one test by name. True if it PASSED (i.e. the mutation survived)."""
    result = subprocess.run(  # noqa: S603
        [str(VENV_PYTHON), "-m", "pytest", "-q", "-k", selector, "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
    )
    return result.returncode == 0


def apply_mutation(mutation: Mutation) -> bool:
    original = mutation.path.read_text(encoding="utf-8")
    if mutation.find not in original:
        return False
    mutation.path.write_text(
        original.replace(mutation.find, mutation.replace, 1), encoding="utf-8"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutations only")
    parser.add_argument("-k", dest="filter", default="", help="substring filter")
    args = parser.parse_args()

    selected = [
        m
        for m in MUTATIONS
        if not args.filter
        or args.filter in m.name
        or args.filter in m.invariant.lower()
    ]

    if args.list:
        for m in selected:
            print(f"  {m.invariant:<4} {m.name:<40} -> {m.expect_failing}")
        return 0

    if not VENV_PYTHON.exists():
        print(f"error: {VENV_PYTHON} not found; run `make venv` first", file=sys.stderr)
        return 1

    survived: list[Mutation] = []
    not_applied: list[Mutation] = []
    killed = 0

    print(f"running {len(selected)} mutations\n")
    with tempfile.TemporaryDirectory() as tmp:
        backup_root = Path(tmp)
        for mutation in selected:
            backup = backup_root / mutation.name
            shutil.copy2(mutation.path, backup)
            try:
                if not apply_mutation(mutation):
                    not_applied.append(mutation)
                    print(f"  ?? {mutation.name:<44} pattern no longer present")
                    continue
                # The mutation is killed when the guarding test FAILS.
                if run_suite(mutation.expect_failing):
                    survived.append(mutation)
                    print(f"  SURVIVED {mutation.name:<40} [{mutation.invariant}]")
                else:
                    killed += 1
                    print(f"  killed   {mutation.name:<40} [{mutation.invariant}]")
            finally:
                shutil.copy2(backup, mutation.path)

    print(f"\nkilled {killed}/{len(selected) - len(not_applied)}")

    if not_applied:
        print("\nMutations whose target code has moved — the mutation, not the")
        print("code, is out of date and must be re-pointed:")
        for m in not_applied:
            print(f"  {m.name} ({m.file})")

    if survived:
        print("\nBLIND SPOTS — the control was removed and the suite stayed green:")
        for m in survived:
            print(f"  {m.invariant} {m.name}")
            print(f"    expected {m.expect_failing} to fail; it passed")
        return 1

    return 1 if not_applied else 0


if __name__ == "__main__":
    raise SystemExit(main())
