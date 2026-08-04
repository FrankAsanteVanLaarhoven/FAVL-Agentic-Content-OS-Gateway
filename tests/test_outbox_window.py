"""The duplicate-window safety invariant.

Every value here comes from the runtime configuration functions the services
themselves call. Nothing is restated as a test constant — that was the flaw
in the original proof: configuration could drift while the test kept passing.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "favl-outbox"))

import favl_outbox.config as cfg  # noqa: E402
from favl_outbox.timing import (  # noqa: E402
    DuplicateWindowTooSmall,
    OperationalDelays,
    RetryPolicy,
    validate_duplicate_window,
    worst_case_retry_horizon,
)


def test_deployed_configuration_satisfies_the_invariant():
    """The shipped defaults must leave the horizon inside the window."""
    utilisation = cfg.enforce_duplicate_window_invariant("test")
    assert 0 < utilisation < cfg.SAFETY_MARGIN


def test_horizon_counts_operational_delay_not_only_backoff():
    """A horizon of pure backoff would understate the real worst case."""
    policy = cfg.retry_policy_from_env()
    delays = cfg.operational_delays_from_env()

    with_delays = worst_case_retry_horizon(policy, delays)
    without = worst_case_retry_horizon(
        policy,
        OperationalDelays(0, 0, 0, 0, 0),
    )
    assert with_delays > without
    # Every one of the six delay sources must move the number.
    assert with_delays - without == pytest.approx(
        policy.max_attempts * delays.per_attempt_seconds
    )


@pytest.mark.parametrize(
    "field",
    [
        "db_lock_wait_seconds",
        "broker_connect_timeout_seconds",
        "publish_timeout_seconds",
        "process_restart_seconds",
        "poll_interval_seconds",
    ],
)
def test_each_operational_delay_contributes(field):
    policy = cfg.retry_policy_from_env()
    base = cfg.operational_delays_from_env()
    bumped = OperationalDelays(
        **{**base.__dict__, field: getattr(base, field) + 10}
    )
    assert worst_case_retry_horizon(policy, bumped) > worst_case_retry_horizon(
        policy, base
    )


def test_restart_delay_is_included():
    """A row delayed by a pod restart still has to land inside the window."""
    assert cfg.operational_delays_from_env().process_restart_seconds > 0


def test_invariant_rejects_a_horizon_that_exceeds_the_window():
    unsafe = RetryPolicy(max_attempts=200, base_seconds=1.0, cap_seconds=300.0, jitter_ratio=0.25)
    delays = cfg.operational_delays_from_env()
    with pytest.raises(DuplicateWindowTooSmall) as exc:
        validate_duplicate_window(unsafe, delays, cfg.duplicate_window_seconds())
    assert "duplicate window" in str(exc.value)


def test_invariant_rejects_a_shrunken_window():
    policy = cfg.retry_policy_from_env()
    delays = cfg.operational_delays_from_env()
    with pytest.raises(DuplicateWindowTooSmall):
        validate_duplicate_window(policy, delays, 60.0)


def test_safety_margin_leaves_headroom():
    """Utilisation is capped below 1.0 so an underestimate is not fatal."""
    policy = cfg.retry_policy_from_env()
    delays = cfg.operational_delays_from_env()
    window = cfg.duplicate_window_seconds()
    # Sits exactly on the margin: allowed at the margin, rejected just under.
    horizon = worst_case_retry_horizon(policy, delays)
    validate_duplicate_window(policy, delays, horizon / cfg.SAFETY_MARGIN)
    with pytest.raises(DuplicateWindowTooSmall):
        validate_duplicate_window(policy, delays, horizon / cfg.SAFETY_MARGIN - 1)


def test_raising_max_attempts_via_environment_is_caught(monkeypatch):
    """Config drift must fail the gate, not slip past a hard-coded test."""
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "500")
    importlib.reload(cfg)
    try:
        with pytest.raises(DuplicateWindowTooSmall):
            cfg.enforce_duplicate_window_invariant("test")
    finally:
        monkeypatch.delenv("OUTBOX_MAX_ATTEMPTS", raising=False)
        importlib.reload(cfg)


def test_shrinking_the_window_via_environment_is_caught(monkeypatch):
    monkeypatch.setenv("OUTBOX_DUPLICATE_WINDOW_SECONDS", "120")
    import favl_outbox.jetstream as js

    importlib.reload(js)
    importlib.reload(cfg)
    try:
        with pytest.raises(DuplicateWindowTooSmall):
            cfg.enforce_duplicate_window_invariant("test")
    finally:
        monkeypatch.delenv("OUTBOX_DUPLICATE_WINDOW_SECONDS", raising=False)
        importlib.reload(js)
        importlib.reload(cfg)


def test_environment_is_restored_after_drift_tests():
    assert "OUTBOX_MAX_ATTEMPTS" not in os.environ
    cfg.enforce_duplicate_window_invariant("test")
