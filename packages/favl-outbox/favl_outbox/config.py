"""Runtime configuration and the startup safety gate.

Everything here is read from the environment the service actually runs with,
so the duplicate-window proof is performed against deployed values. A test
that hard-codes the same numbers proves nothing once configuration drifts.
"""

from __future__ import annotations

import logging
import os

from . import metrics
from .jetstream import (
    CONNECT_TIMEOUT_SECONDS,
    DUPLICATE_WINDOW_SECONDS,
    PUBLISH_TIMEOUT_SECONDS,
)
from .publisher import DEFAULT_RETRY_POLICY
from .timing import (
    OperationalDelays,
    RetryPolicy,
    validate_duplicate_window,
    worst_case_retry_horizon,
)

logger = logging.getLogger(__name__)

SAFETY_MARGIN = float(os.getenv("OUTBOX_WINDOW_SAFETY_MARGIN", "0.75"))


def retry_policy_from_env() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=int(os.getenv("OUTBOX_MAX_ATTEMPTS", str(DEFAULT_RETRY_POLICY.max_attempts))),
        base_seconds=float(os.getenv("OUTBOX_BACKOFF_BASE", str(DEFAULT_RETRY_POLICY.base_seconds))),
        cap_seconds=float(os.getenv("OUTBOX_BACKOFF_CAP", str(DEFAULT_RETRY_POLICY.cap_seconds))),
        jitter_ratio=float(os.getenv("OUTBOX_JITTER_RATIO", str(DEFAULT_RETRY_POLICY.jitter_ratio))),
    )


def operational_delays_from_env() -> OperationalDelays:
    """Upper bounds on delay a row can accumulate outside the backoff curve."""
    return OperationalDelays(
        db_lock_wait_seconds=float(os.getenv("OUTBOX_DB_LOCK_WAIT", "5")),
        broker_connect_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
        publish_timeout_seconds=PUBLISH_TIMEOUT_SECONDS,
        # Time for a killed process to be rescheduled and resume draining.
        process_restart_seconds=float(os.getenv("OUTBOX_RESTART_BUDGET", "60")),
        poll_interval_seconds=float(os.getenv("OUTBOX_IDLE_INTERVAL", "1.0")),
    )


def duplicate_window_seconds() -> float:
    return DUPLICATE_WINDOW_SECONDS


def enforce_duplicate_window_invariant(service: str) -> float:
    """Release-blocking gate. Raises DuplicateWindowTooSmall if unsafe.

    Called during startup, before the publisher accepts work, so a service
    configured such that a retry could escape deduplication fails loudly
    instead of silently delivering an event twice months later.
    """
    policy = retry_policy_from_env()
    delays = operational_delays_from_env()
    window = duplicate_window_seconds()

    utilisation = validate_duplicate_window(policy, delays, window, SAFETY_MARGIN)
    horizon = worst_case_retry_horizon(policy, delays)

    metrics.WINDOW_UTILISATION.labels(service).set(utilisation)
    logger.info(
        "outbox.window_invariant_ok service=%s horizon=%.0fs window=%.0fs "
        "utilisation=%.1f%% margin=%.0f%%",
        service,
        horizon,
        window,
        utilisation * 100,
        SAFETY_MARGIN * 100,
    )
    return utilisation
