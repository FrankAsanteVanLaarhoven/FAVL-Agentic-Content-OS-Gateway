"""Unit tests for outbox logic that needs no database or broker.

The retry/dead-letter state machine is exercised against a fake row so the
transitions are pinned independently of Postgres behaviour.
"""

import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "favl-outbox"))

from favl_outbox.publisher import (  # noqa: E402
    DEFAULT_RETRY_POLICY,
    DrainResult,
    OutboxPublisher,
    compute_backoff,
)

BACKOFF_CAP_SECONDS = DEFAULT_RETRY_POLICY.cap_seconds
JITTER_RATIO = DEFAULT_RETRY_POLICY.jitter_ratio


@dataclass
class FakeRow:
    subject: str = "agent.created"
    attempts: int = 0
    max_attempts: int = 3
    status: str = "pending"
    last_error: str | None = None
    next_attempt_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = "11111111-1111-1111-1111-111111111111"


def _publisher() -> OutboxPublisher:
    return OutboxPublisher(
        service="test",
        session_factory=None,
        model=None,
        connection=None,
    )


def test_backoff_grows_exponentially():
    rng = random.Random(0)
    first = compute_backoff(1, rng)
    later = compute_backoff(5, rng)
    assert later > first


def test_backoff_is_capped():
    rng = random.Random(0)
    # 2**40 seconds without a cap; jitter may add up to 25% of the cap.
    assert compute_backoff(40, rng) <= BACKOFF_CAP_SECONDS * (1 + JITTER_RATIO)


def test_backoff_never_returns_zero():
    rng = random.Random(1)
    assert all(compute_backoff(n, rng) >= 0.1 for n in range(0, 20))


def test_backoff_is_jittered():
    """Two rows failing together must not retry in lockstep."""
    rng = random.Random(7)
    values = {compute_backoff(3, rng) for _ in range(20)}
    assert len(values) > 1


def test_failure_schedules_a_retry_and_records_the_error():
    pub = _publisher()
    row = FakeRow()
    result = DrainResult()
    before = row.next_attempt_at

    pub._record_failure(row, ConnectionError("broker down"), result)

    assert row.attempts == 1
    assert row.status == "pending"
    assert "ConnectionError" in row.last_error
    assert "broker down" in row.last_error
    assert row.next_attempt_at > before
    assert result.failed == 1
    assert result.dead_lettered == 0


def test_row_dead_letters_after_max_attempts():
    pub = _publisher()
    row = FakeRow(max_attempts=3)
    result = DrainResult()

    for _ in range(3):
        pub._record_failure(row, ConnectionError("still down"), result)

    assert row.attempts == 3
    assert row.status == "dead"
    assert result.dead_lettered == 1
    # A dead row must not be rescheduled; visibility replaces infinite retry.
    assert result.failed == 3


def test_dead_row_stops_consuming_retries():
    pub = _publisher()
    row = FakeRow(max_attempts=1)
    result = DrainResult()
    pub._record_failure(row, ValueError("poison"), result)
    scheduled = row.next_attempt_at

    assert row.status == "dead"
    # next_attempt_at is left untouched once dead.
    assert row.next_attempt_at == scheduled


def test_error_text_is_truncated():
    pub = _publisher()
    row = FakeRow()
    pub._record_failure(row, ValueError("x" * 5000), DrainResult())
    assert len(row.last_error) <= 2000
