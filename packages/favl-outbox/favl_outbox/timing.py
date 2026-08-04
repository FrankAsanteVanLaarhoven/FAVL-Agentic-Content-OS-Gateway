"""Retry horizon arithmetic and the duplicate-window safety invariant.

Deduplication only holds if every retry of an outbox row lands inside the
stream's duplicate window. That is a property of the *deployed* configuration,
not of a constant written into a test, so the horizon is computed here from
the same objects the runtime uses and checked at startup. A service whose
configuration violates the invariant refuses to start.

The horizon deliberately counts more than the backoff curve. A row can be
delayed by operational cost the retry loop knows nothing about: waiting on a
row lock, a broker connect timeout, the publish timeout itself, the poll
interval before the row is even looked at, and a process restart between
attempts. Ignoring those was the gap in the original proof.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_seconds: float
    cap_seconds: float
    jitter_ratio: float

    def backoff_for(self, attempt: int) -> float:
        """Upper bound of the delay scheduled after `attempt` failures."""
        raw = min(self.base_seconds * (2 ** max(attempt - 1, 0)), self.cap_seconds)
        return raw * (1 + self.jitter_ratio)


@dataclass(frozen=True)
class OperationalDelays:
    """Per-attempt cost outside the backoff curve. All upper bounds."""

    db_lock_wait_seconds: float
    broker_connect_timeout_seconds: float
    publish_timeout_seconds: float
    process_restart_seconds: float
    poll_interval_seconds: float

    @property
    def per_attempt_seconds(self) -> float:
        return (
            self.db_lock_wait_seconds
            + self.broker_connect_timeout_seconds
            + self.publish_timeout_seconds
            + self.process_restart_seconds
            + self.poll_interval_seconds
        )


class DuplicateWindowTooSmall(RuntimeError):
    """Configuration would let a retry escape deduplication."""


def worst_case_retry_horizon(
    policy: RetryPolicy, delays: OperationalDelays
) -> float:
    """Longest possible span from first publish attempt to final attempt.

    Every attempt pays the operational cost; every gap between attempts pays
    the jittered backoff. The final attempt schedules no further backoff.
    """
    total = policy.max_attempts * delays.per_attempt_seconds
    for attempt in range(1, policy.max_attempts):
        total += policy.backoff_for(attempt)
    return total


def window_utilisation(
    policy: RetryPolicy, delays: OperationalDelays, duplicate_window_seconds: float
) -> float:
    if duplicate_window_seconds <= 0:
        return float("inf")
    return worst_case_retry_horizon(policy, delays) / duplicate_window_seconds


def validate_duplicate_window(
    policy: RetryPolicy,
    delays: OperationalDelays,
    duplicate_window_seconds: float,
    safety_margin: float = 0.75,
) -> float:
    """Release-blocking check. Returns utilisation, raises if unsafe.

    `safety_margin` keeps headroom: at 0.75 the horizon may consume at most
    three quarters of the window, so an underestimated operational delay does
    not immediately invalidate deduplication.
    """
    horizon = worst_case_retry_horizon(policy, delays)
    limit = duplicate_window_seconds * safety_margin
    if horizon > limit:
        raise DuplicateWindowTooSmall(
            "outbox retry horizon exceeds the safe share of the JetStream "
            f"duplicate window: horizon={horizon:.0f}s "
            f"window={duplicate_window_seconds:.0f}s "
            f"limit={limit:.0f}s ({safety_margin:.0%} of window). "
            "A retry could land outside the window and be delivered twice. "
            "Lower max_attempts or the backoff cap, or raise duplicate_window."
        )
    return horizon / duplicate_window_seconds
