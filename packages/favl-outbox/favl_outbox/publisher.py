"""Background outbox publisher.

Delivery contract
-----------------
At-least-once, deduplicated at the stream.

A row is claimed with `SELECT ... FOR UPDATE SKIP LOCKED` and published while
the claiming transaction is still open. Three outcomes:

* publish acked, transaction commits -> row is `published`, exactly once.
* publish fails -> row stays `pending` with a later `next_attempt_at`.
* publish acked but the process dies before commit -> the transaction rolls
  back and the row returns to `pending`, so it is published a second time.
  That republish carries the same `Nats-Msg-Id` (the row id), so JetStream
  collapses it inside the duplicate window.

The third case is why at-least-once plus dedup is the guarantee rather than
exactly-once. There is no way to commit a database transaction and a broker
publish atomically without a distributed transaction; the duplicate window is
what makes the resulting retry harmless.

`SKIP LOCKED` means several replicas can run this loop concurrently without
double-claiming a row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import metrics
from .models import STATUS_DEAD, STATUS_PENDING, STATUS_PUBLISHED

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 300.0
JITTER_RATIO = 0.25


def compute_backoff(attempts: int, rng: random.Random | None = None) -> float:
    """Exponential backoff with proportional jitter.

    Jitter prevents a broker outage from producing a synchronised retry storm
    when many rows become due at the same instant.
    """
    rng = rng or random
    raw = min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_SECONDS)
    jitter = raw * JITTER_RATIO
    return max(0.1, raw + rng.uniform(-jitter, jitter))


@dataclass
class DrainResult:
    claimed: int = 0
    published: int = 0
    failed: int = 0
    dead_lettered: int = 0
    subjects: list[str] = field(default_factory=list)


@dataclass
class OutboxStats:
    pending: int = 0
    dead: int = 0
    published: int = 0
    oldest_pending_age_seconds: float | None = None


class OutboxPublisher:
    def __init__(
        self,
        *,
        service: str,
        session_factory: async_sessionmaker,
        model: Any,
        connection: Any,
        batch_size: int = 100,
        poll_interval: float = 0.25,
        idle_interval: float = 1.0,
    ) -> None:
        self.service = service
        self._session_factory = session_factory
        self._model = model
        self._conn = connection
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._idle_interval = idle_interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"outbox-{self.service}")
        logger.info("outbox.publisher_started service=%s", self.service)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("outbox.publisher_stopped service=%s", self.service)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = await self.drain_once()
                await self.refresh_stats()
                # Back off when idle so an empty outbox does not spin the CPU,
                # but keep polling tight while a backlog is draining.
                delay = self._poll_interval if result.claimed else self._idle_interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("outbox.drain_error service=%s", self.service)
                delay = self._idle_interval
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except (TimeoutError, asyncio.TimeoutError):
                pass

    # ------------------------------------------------------------------ #
    # draining
    # ------------------------------------------------------------------ #

    async def drain_once(self) -> DrainResult:
        """Claim and publish one batch. Safe to call from tests directly."""
        result = DrainResult()
        started = time.perf_counter()
        now = datetime.now(timezone.utc)

        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            select(self._model)
                            .where(
                                self._model.status == STATUS_PENDING,
                                self._model.next_attempt_at <= now,
                            )
                            .order_by(self._model.created_at)
                            .limit(self._batch_size)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                result.claimed = len(rows)

                for row in rows:
                    await self._publish_row(session, row, result)

        metrics.DRAIN_DURATION.labels(self.service).observe(time.perf_counter() - started)
        return result

    async def _publish_row(self, session: Any, row: Any, result: DrainResult) -> None:
        body = json.dumps(row.payload, default=str).encode()
        started = time.perf_counter()
        try:
            ack = await self._conn.publish(row.subject, body, msg_id=str(row.id))
        except Exception as exc:
            self._record_failure(row, exc, result)
            return

        metrics.PUBLISH_DURATION.labels(self.service).observe(time.perf_counter() - started)
        row.status = STATUS_PUBLISHED
        row.published_at = datetime.now(timezone.utc)
        row.stream_seq = getattr(ack, "seq", None)
        row.last_error = None
        result.published += 1
        result.subjects.append(row.subject)
        metrics.PUBLISHED.labels(self.service, row.subject).inc()
        self.last_error = None
        logger.info(
            "outbox.published service=%s id=%s subject=%s seq=%s duplicate=%s",
            self.service,
            row.id,
            row.subject,
            row.stream_seq,
            getattr(ack, "duplicate", False),
        )

    def _record_failure(self, row: Any, exc: Exception, result: DrainResult) -> None:
        row.attempts += 1
        row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        result.failed += 1
        metrics.FAILURES.labels(self.service, row.subject).inc()
        self.last_error = row.last_error

        if row.attempts >= row.max_attempts:
            row.status = STATUS_DEAD
            result.dead_lettered += 1
            metrics.DEAD_LETTERED.labels(self.service, row.subject).inc()
            logger.error(
                "outbox.dead_lettered service=%s id=%s subject=%s attempts=%d error=%s",
                self.service,
                row.id,
                row.subject,
                row.attempts,
                row.last_error,
            )
            return

        delay = compute_backoff(row.attempts)
        row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logger.warning(
            "outbox.publish_failed service=%s id=%s subject=%s attempt=%d/%d "
            "retry_in=%.1fs error=%s",
            self.service,
            row.id,
            row.subject,
            row.attempts,
            row.max_attempts,
            delay,
            row.last_error,
        )

    # ------------------------------------------------------------------ #
    # observability
    # ------------------------------------------------------------------ #

    async def stats(self) -> OutboxStats:
        async with self._session_factory() as session:
            counts = dict(
                (
                    await session.execute(
                        select(self._model.status, func.count()).group_by(
                            self._model.status
                        )
                    )
                ).all()
            )
            oldest = (
                await session.execute(
                    select(func.min(self._model.created_at)).where(
                        self._model.status == STATUS_PENDING
                    )
                )
            ).scalar()

        age = None
        if oldest is not None:
            age = (datetime.now(timezone.utc) - oldest).total_seconds()

        return OutboxStats(
            pending=int(counts.get(STATUS_PENDING, 0)),
            dead=int(counts.get(STATUS_DEAD, 0)),
            published=int(counts.get(STATUS_PUBLISHED, 0)),
            oldest_pending_age_seconds=age,
        )

    async def refresh_stats(self) -> OutboxStats:
        current = await self.stats()
        metrics.PENDING.labels(self.service).set(current.pending)
        metrics.DEAD.labels(self.service).set(current.dead)
        metrics.OLDEST_PENDING_AGE.labels(self.service).set(
            current.oldest_pending_age_seconds or 0.0
        )
        return current
