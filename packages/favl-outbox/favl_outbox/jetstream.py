"""JetStream connection shared by the outbox publishers."""

from __future__ import annotations

import logging
import os
from typing import Any

import nats
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

logger = logging.getLogger(__name__)

STREAM_NAME = "FAVL_EVENTS"
SUBJECT_PREFIX = "favl"

# Must exceed the worst-case retry horizon of the outbox publisher, otherwise
# a late republish after a crash escapes deduplication. The relationship is
# not asserted here by eye — favl_outbox.timing computes the horizon from the
# deployed retry policy and operational delays, and the publisher refuses to
# start if it breaches the safety margin.
DUPLICATE_WINDOW_SECONDS = float(
    os.getenv("OUTBOX_DUPLICATE_WINDOW_SECONDS", str(2 * 3600))
)

# Upper bounds for the operational cost the retry loop itself cannot see.
PUBLISH_TIMEOUT_SECONDS = float(os.getenv("OUTBOX_PUBLISH_TIMEOUT", "5"))
CONNECT_TIMEOUT_SECONDS = float(os.getenv("OUTBOX_CONNECT_TIMEOUT", "5"))


class JetStreamConnection:
    def __init__(self) -> None:
        self._nc: Any = None
        self._js: Any = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return bool(self._nc and self._nc.is_connected)

    @property
    def ready(self) -> bool:
        return self.connected and self._js is not None

    async def connect(self) -> None:
        url = os.getenv("NATS_URL", "nats://localhost:4222")
        try:
            self._nc = await nats.connect(
                url,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
            )
            self._js = self._nc.jetstream()
            await self._ensure_stream()
            self.last_error = None
            logger.info("nats.connected url=%s stream=%s", url, STREAM_NAME)
        except Exception as exc:
            # A broker that is down at boot must not stop the API from serving.
            # Writes still commit; the outbox drains once the broker returns.
            self._nc = None
            self._js = None
            self.last_error = f"connect failed: {exc}"
            logger.error("nats.connect_failed url=%s error=%s", url, exc)

    async def _ensure_stream(self) -> None:
        config = StreamConfig(
            name=STREAM_NAME,
            subjects=[f"{SUBJECT_PREFIX}.>"],
            retention=RetentionPolicy.LIMITS,
            storage=StorageType.FILE,
            discard=DiscardPolicy.OLD,
            max_age=7 * 24 * 3600,
            duplicate_window=DUPLICATE_WINDOW_SECONDS,
            num_replicas=1,
        )
        try:
            await self._js.stream_info(STREAM_NAME)
        except NotFoundError:
            await self._js.add_stream(config)
            logger.info("nats.stream_created name=%s", STREAM_NAME)
            return
        # Both services call this; update is idempotent and reconciles the
        # duplicate window onto a stream created before dedup was introduced.
        await self._js.update_stream(config)

    async def publish(
        self,
        subject: str,
        body: bytes,
        msg_id: str,
        timeout: float = PUBLISH_TIMEOUT_SECONDS,
    ) -> Any:
        """Publish with a deduplication id. Raises on failure."""
        if not self.ready:
            raise ConnectionError("jetstream unavailable")
        return await self._js.publish(
            f"{SUBJECT_PREFIX}.{subject}",
            body,
            timeout=timeout,
            headers={"Nats-Msg-Id": msg_id},
        )

    async def close(self) -> None:
        if self._nc and self._nc.is_connected:
            await self._nc.drain()
        self._nc = None
        self._js = None
