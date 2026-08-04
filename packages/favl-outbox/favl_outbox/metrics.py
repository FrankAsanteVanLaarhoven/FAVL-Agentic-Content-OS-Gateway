"""Prometheus metrics for outbox visibility.

Naming note: `favl_outbox_pending` is a gauge, so it deliberately does not
carry a `_total` suffix. Prometheus reserves `_total` for counters, and
promtool lints a `_total` gauge as an error. Counters below keep the suffix.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --------------------------------------------------------------------- #
# counters
# --------------------------------------------------------------------- #

PUBLISH = Counter(
    "favl_outbox_publish_total",
    "Outbox publish attempts by outcome.",
    ["service", "subject", "result"],  # result: success | failure
)

RETRY = Counter(
    "favl_outbox_retry_total",
    "Outbox rows rescheduled for another attempt.",
    ["service", "subject"],
)

DEAD_LETTER = Counter(
    "favl_outbox_dead_letter_total",
    "Outbox rows moved to dead after exhausting retries.",
    ["service", "subject"],
)

CLAIMED = Counter(
    "favl_outbox_claimed_total",
    "Outbox rows claimed by a publisher batch.",
    ["service"],
)

CLAIM_TIMEOUT = Counter(
    "favl_outbox_claim_timeout_total",
    "Publisher batches that failed to claim rows, including lock timeouts.",
    ["service"],
)

# --------------------------------------------------------------------- #
# gauges
# --------------------------------------------------------------------- #

PENDING = Gauge(
    "favl_outbox_pending",
    "Outbox events awaiting publication.",
    ["service"],
)

DEAD = Gauge(
    "favl_outbox_dead",
    "Outbox events in the dead state, awaiting operator action.",
    ["service"],
)

OLDEST_PENDING = Gauge(
    "favl_outbox_oldest_pending_seconds",
    "Age of the oldest unpublished outbox event. The primary backlog alert.",
    ["service"],
)

WINDOW_UTILISATION = Gauge(
    "favl_outbox_duplicate_window_utilisation",
    "Worst-case retry horizon as a fraction of the JetStream duplicate "
    "window. Above 1.0 a retry could escape deduplication.",
    ["service"],
)

# --------------------------------------------------------------------- #
# histograms
# --------------------------------------------------------------------- #

PUBLISH_LATENCY = Histogram(
    "favl_outbox_publish_latency_seconds",
    "Time to publish a single outbox event and receive the ack.",
    ["service"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

DRAIN_LATENCY = Histogram(
    "favl_outbox_drain_latency_seconds",
    "Time to process one publisher batch.",
    ["service"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
