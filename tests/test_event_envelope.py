"""The published event envelope, which is the consumer idempotency contract.

Broker deduplication expires. These fields are what lets a consumer stay
correct when it does.
"""

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "favl-outbox"))

from favl_outbox.envelope import SCHEMA_VERSION, envelope_from_row  # noqa: E402

REQUIRED_FIELDS = {
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "aggregate_version",
    "occurred_at",
    "schema_version",
    "data",
}


@dataclass
class FakeRow:
    id: str = "7f2b1c64-0a3d-4a5e-9c11-2b3c4d5e6f70"
    subject: str = "agent.created"
    aggregate_type: str = "agent"
    aggregate_id: str = "aggregate-1"
    aggregate_version: int = 3
    schema_version: int = SCHEMA_VERSION
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    )
    payload: dict[str, Any] = field(default_factory=lambda: {"name": "research"})


def test_envelope_exposes_the_full_contract():
    assert set(envelope_from_row(FakeRow())) == REQUIRED_FIELDS


def test_event_id_is_the_outbox_row_id():
    """It is also the Nats-Msg-Id, so both dedup layers key on one value."""
    row = FakeRow()
    assert envelope_from_row(row)["event_id"] == str(row.id)


def test_event_id_is_stable_across_republication():
    row = FakeRow()
    assert envelope_from_row(row)["event_id"] == envelope_from_row(row)["event_id"]


def test_aggregate_version_is_carried_for_ordering():
    assert envelope_from_row(FakeRow(aggregate_version=7))["aggregate_version"] == 7


def test_domain_body_is_nested_under_data():
    """Envelope fields must not collide with domain field names."""
    env = envelope_from_row(FakeRow(payload={"event_id": "not-the-envelope-id"}))
    assert env["event_id"] == FakeRow().id
    assert env["data"]["event_id"] == "not-the-envelope-id"


def test_occurred_at_is_iso_with_timezone():
    value = envelope_from_row(FakeRow())["occurred_at"]
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None


def test_schema_version_is_present_for_forward_compatibility():
    assert envelope_from_row(FakeRow())["schema_version"] == SCHEMA_VERSION


def test_event_type_matches_the_subject():
    assert envelope_from_row(FakeRow(subject="connector.deleted"))["event_type"] == (
        "connector.deleted"
    )
