"""Prometheus metrics for outbox visibility."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

PUBLISHED = Counter(
    "favl_outbox_published_total",
    "Outbox events acknowledged by JetStream.",
    ["service", "subject"],
)

FAILURES = Counter(
    "favl_outbox_publish_failures_total",
    "Outbox publish attempts that failed.",
    ["service", "subject"],
)

DEAD_LETTERED = Counter(
    "favl_outbox_dead_lettered_total",
    "Outbox events moved to dead after exhausting retries.",
    ["service", "subject"],
)

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

OLDEST_PENDING_AGE = Gauge(
    "favl_outbox_oldest_pending_age_seconds",
    "Age of the oldest unpublished outbox event.",
    ["service"],
)

PUBLISH_DURATION = Histogram(
    "favl_outbox_publish_duration_seconds",
    "Time to publish a single outbox event and receive the ack.",
    ["service"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

DRAIN_DURATION = Histogram(
    "favl_outbox_drain_duration_seconds",
    "Time to process one publisher batch.",
    ["service"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
