"""Consumer-side parsing, including compatibility with legacy flat events.

The stream still holds pre-envelope events from earlier milestones. They are
kept deliberately — they are replay and migration-test material — so this
compatibility is a release gate, not a temporary shim. Removing it requires
an explicit migration decision.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "favl-outbox"))

from favl_outbox.consumer import (  # noqa: E402
    APPLY,
    IGNORE,
    LEGACY_SCHEMA_VERSION,
    QUARANTINE,
    UnsupportedSchemaVersion,
    parse_event,
    version_decision,
)

# Exactly the shape M1.1 published, before the envelope existed.
LEGACY_AGENT_CREATED = {
    "id": "9b7c1f22-0000-4000-8000-000000000001",
    "name": "research-agent",
    "description": "",
    "connector_ids": [],
    "created_at": "2026-08-04T05:00:00Z",
}

V1_EVENT = {
    "event_id": "11111111-2222-4333-8444-555555555555",
    "event_type": "agent.created",
    "aggregate_type": "agent",
    "aggregate_id": "9b7c1f22-0000-4000-8000-000000000001",
    "aggregate_version": 1,
    "occurred_at": "2026-08-04T05:00:00+00:00",
    "schema_version": 1,
    "data": {"name": "research-agent"},
}


def test_legacy_event_is_identified_as_version_zero():
    """A missing schema_version must not be silently treated as version 1."""
    event = parse_event(LEGACY_AGENT_CREATED, subject="favl.agent.created")
    assert event.schema_version == LEGACY_SCHEMA_VERSION
    assert event.schema_version != 1
    assert event.is_legacy


def test_legacy_event_still_yields_usable_data():
    event = parse_event(LEGACY_AGENT_CREATED, subject="favl.agent.created")
    assert event.event_type == "favl.agent.created"
    assert event.data["name"] == "research-agent"
    assert event.aggregate_id == LEGACY_AGENT_CREATED["id"]


def test_legacy_event_admits_it_cannot_be_deduplicated():
    """The old format carried no event_id; nothing is invented to fill it."""
    event = parse_event(LEGACY_AGENT_CREATED, subject="favl.agent.created")
    assert event.event_id is None
    assert not event.is_deduplicable
    assert not event.is_orderable


def test_v1_event_parses_into_the_same_internal_shape():
    event = parse_event(V1_EVENT)
    assert event.schema_version == 1
    assert event.event_id == V1_EVENT["event_id"]
    assert event.aggregate_version == 1
    assert event.is_deduplicable and event.is_orderable
    assert event.data == {"name": "research-agent"}


def test_unknown_future_version_is_rejected_not_guessed():
    with pytest.raises(UnsupportedSchemaVersion) as exc:
        parse_event({**V1_EVENT, "schema_version": 99})
    assert exc.value.version == 99


def test_occurred_at_parses_with_a_trailing_z():
    assert parse_event(LEGACY_AGENT_CREATED, "s").occurred_at.tzinfo is not None


# ------------------------------------------------------------------ #
# aggregate ordering
# ------------------------------------------------------------------ #


def test_next_version_is_applied():
    assert version_decision(2, 1) == APPLY


def test_first_version_is_applied():
    assert version_decision(1, None) == APPLY


@pytest.mark.parametrize("incoming", [1, 2, 3])
def test_replayed_or_stale_versions_are_ignored_idempotently(incoming):
    assert version_decision(incoming, 3) == IGNORE


def test_a_gap_is_quarantined_rather_than_accepted():
    """A higher version is not automatically safe.

    A gap means a missed event or an incomplete replay; applying it would
    silently skip intermediate state.
    """
    assert version_decision(5, 1) == QUARANTINE


def test_a_first_event_that_is_not_version_one_is_quarantined():
    assert version_decision(4, None) == QUARANTINE


def test_unorderable_legacy_event_is_quarantined():
    assert version_decision(None, 1) == QUARANTINE


def test_malformed_v1_envelope_is_classified_not_crashed():
    """A missing required field must raise the declared type and be counted.

    It previously raised a bare KeyError, which escaped a consumer's
    `except UnsupportedSchemaVersion`, killed the handler, and left the
    rejection counter untouched — an invisible drop.
    """
    from favl_outbox.consumer import (
        SCHEMA_REJECTED,
        EventParseError,
        MalformedEnvelope,
    )

    def rejected() -> float:
        return SCHEMA_REJECTED.labels("malformed_envelope")._value.get()

    before = rejected()
    with pytest.raises(MalformedEnvelope):
        parse_event({"schema_version": 1, "event_type": "agent.created", "data": {}})
    assert rejected() == before + 1

    # A consumer catching the base type covers every rejection.
    with pytest.raises(EventParseError):
        parse_event({"schema_version": 1, "event_id": "x", "data": {}})
    with pytest.raises(EventParseError):
        parse_event({"schema_version": 99})
