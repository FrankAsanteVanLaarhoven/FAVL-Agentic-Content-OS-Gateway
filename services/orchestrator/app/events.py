"""JetStream event publishing.

Core NATS publish is fire-and-forget: if no consumer is attached, or the
server drops the message, the publisher never learns. JetStream persists the
message to the stream and returns a PubAck carrying the assigned sequence, so
a publish that returns without raising is durably stored.

Known gap: the write to Postgres and the publish here are two separate
operations. A crash between them loses the event. Closing that requires a
transactional outbox (write the event to a table in the same transaction as
the entity, drain it with a background worker). Until then a publish failure
is logged and surfaced through /health/ready, never silently swallowed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import nats
from nats.errors import Error as NatsError
from nats.js.api import RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

STREAM_NAME = "FAVL_EVENTS"
SUBJECT_PREFIX = "favl"


class EventPublisher:
    """Owns the NATS connection and the JetStream context."""

    def __init__(self) -> None:
        self._nc: nats.NATS | None = None
        self._js: Any = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return bool(self._nc and self._nc.is_connected)

    @property
    def stream_ready(self) -> bool:
        return self._js is not None

    async def connect(self) -> None:
        url = os.getenv("NATS_URL", "nats://localhost:4222")
        try:
            self._nc = await nats.connect(
                url,
                connect_timeout=5,
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
            )
            self._js = self._nc.jetstream()
            await self._ensure_stream()
            self.last_error = None
        except Exception as exc:  # broker unavailable at boot is not fatal
            self._nc = None
            self._js = None
            self.last_error = f"connect failed: {exc}"
            logger.error("nats.connect_failed url=%s error=%s", url, exc)

    async def _ensure_stream(self) -> None:
        """Create the stream once; tolerate it already existing."""
        try:
            await self._js.stream_info(STREAM_NAME)
        except NotFoundError:
            await self._js.add_stream(
                StreamConfig(
                    name=STREAM_NAME,
                    subjects=[f"{SUBJECT_PREFIX}.>"],
                    retention=RetentionPolicy.LIMITS,
                    storage=StorageType.FILE,
                    max_age=7 * 24 * 3600,
                    num_replicas=1,
                )
            )
            logger.info("nats.stream_created name=%s", STREAM_NAME)

    async def publish(self, subject: str, payload: dict[str, Any]) -> bool:
        """Publish and wait for the server ack. Returns success."""
        full_subject = f"{SUBJECT_PREFIX}.{subject}"
        if not self.stream_ready:
            self.last_error = "jetstream unavailable"
            logger.error("event.publish_skipped subject=%s reason=no_jetstream", full_subject)
            return False

        body = json.dumps(payload, default=str).encode()
        try:
            ack = await self._js.publish(full_subject, body, timeout=5)
        except (NatsError, TimeoutError) as exc:
            self.last_error = f"publish failed: {exc}"
            logger.error("event.publish_failed subject=%s error=%s", full_subject, exc)
            return False

        logger.info(
            "event.published subject=%s stream=%s seq=%s",
            full_subject,
            ack.stream,
            ack.seq,
        )
        self.last_error = None
        return True

    async def close(self) -> None:
        if self._nc and self._nc.is_connected:
            await self._nc.drain()
        self._nc = None
        self._js = None


publisher = EventPublisher()
