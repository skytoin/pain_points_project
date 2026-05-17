"""Tests for `discovery.workers.worker` — the task-queue bridge.

Layered to keep failures pinpointable:

1. `claim_one(session)` — atomic claim of one queued task.
2. `run_one(session, registry, task)` — dispatch + persist + mark done.
3. `run_worker_once(session, registry)` — the two above, glued.

All tests use in-memory SQLite via `create_engine_for(...)` so there's
no global state between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 — registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, RawRecordRow, Task, TaskStatus
from discovery.sources.base import BaseSource, RawRecord
from discovery.workers.worker import (
    SourceRegistry,
    aclose_registry,
    claim_one,
    run_one,
    run_worker_drain,
    run_worker_once,
    sweep_stuck_tasks,
)

# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A fresh in-memory async SQLite session for each test.

    Uses `async_session_factory` so we inherit `expire_on_commit=False`
    — otherwise SQLAlchemy expires attributes on every commit and the
    next attribute access tries to lazy-load (sync I/O in an async
    context → `MissingGreenlet`).
    """
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()


async def _make_job(session: AsyncSession) -> Job:
    job = Job(spec_hash="j" * 64, spec={})
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def _queue_task(
    session: AsyncSession,
    job_id: int,
    *,
    wave: int = 1,
    source: str = "reddit",
    action: str = "fetch",
    params: dict[str, Any] | None = None,
    content_hash: str | None = None,
    max_attempts: int = 3,
    attempts: int = 0,
) -> Task:
    task = Task(
        job_id=job_id,
        wave=wave,
        source=source,
        action=action,
        params=params if params is not None else {},
        content_hash=content_hash if content_hash is not None else ("c" * 64),
        max_attempts=max_attempts,
        attempts=attempts,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# --- Fake sources for `run_one` tests --------------------------------------


class FakeSource(BaseSource):
    """A controllable source — returns whatever records you hand it."""

    name = "fake"
    rate_limit = (1000, 60)

    def __init__(self, records: list[RawRecord]) -> None:
        self._records = records

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        return list(self._records)


class FailingSource(BaseSource):
    """A source that always raises — for testing retry/fail paths."""

    name = "fake"
    rate_limit = (1000, 60)

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("boom")

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        raise self._exc


def _record(external_id: str, body: dict[str, Any] | None = None) -> RawRecord:
    return RawRecord(
        source="fake",
        external_id=external_id,
        body=body if body is not None else {"title": external_id},
    )


# --- claim_one --------------------------------------------------------------


class TestClaimOne:
    async def test_returns_none_when_queue_empty(self, session: AsyncSession) -> None:
        assert await claim_one(session) is None

    async def test_flips_queued_task_to_running(self, session: AsyncSession) -> None:
        job = await _make_job(session)
        await _queue_task(session, job.id)

        claimed = await claim_one(session)

        assert claimed is not None
        assert claimed.status == TaskStatus.running
        assert claimed.claimed_at is not None
        assert claimed.attempts == 1

    async def test_skips_already_running_tasks(self, session: AsyncSession) -> None:
        """A second claim against a queue with only running tasks returns None."""
        job = await _make_job(session)
        await _queue_task(session, job.id)
        first = await claim_one(session)
        assert first is not None

        # No more queued rows — second claim should find nothing.
        assert await claim_one(session) is None

    async def test_orders_by_wave_ascending(self, session: AsyncSession) -> None:
        """Lower wave wins, regardless of insertion order."""
        job = await _make_job(session)
        await _queue_task(session, job.id, wave=3, content_hash="a" * 64)
        await _queue_task(session, job.id, wave=1, content_hash="b" * 64)
        await _queue_task(session, job.id, wave=2, content_hash="c" * 64)

        first = await claim_one(session)
        assert first is not None
        assert first.wave == 1

    async def test_orders_by_created_at_within_wave(self, session: AsyncSession) -> None:
        """Within a wave, oldest task goes first (FIFO)."""
        job = await _make_job(session)
        older = await _queue_task(session, job.id, wave=1, content_hash="a" * 64)
        await _queue_task(session, job.id, wave=1, content_hash="b" * 64)

        first = await claim_one(session)
        assert first is not None
        assert first.id == older.id


# --- run_one ----------------------------------------------------------------


class TestRunOne:
    async def test_dispatches_to_registered_source(self, session: AsyncSession) -> None:
        """The worker looks up the adapter in the registry by `task.source`."""
        job = await _make_job(session)
        task = await _queue_task(session, job.id, source="fake")
        task = await claim_one(session)
        assert task is not None

        seen: dict[str, Any] = {}

        class CapturingSource(BaseSource):
            name = "fake"
            rate_limit = (1000, 60)

            async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
                seen["params"] = params
                return []

        registry: SourceRegistry = {"fake": CapturingSource()}
        await run_one(session, registry, task)

        assert seen["params"] == {}

    async def test_persists_returned_records_to_bronze(self, session: AsyncSession) -> None:
        job = await _make_job(session)
        await _queue_task(session, job.id, source="fake")
        task = await claim_one(session)
        assert task is not None

        registry: SourceRegistry = {"fake": FakeSource([_record("ext-1"), _record("ext-2")])}
        await run_one(session, registry, task)

        result = await session.exec(select(RawRecordRow))
        rows = list(result.all())
        assert len(rows) == 2
        external_ids = {r.external_id for r in rows}
        assert external_ids == {"ext-1", "ext-2"}
        for r in rows:
            assert r.job_id == job.id
            assert r.task_id == task.id
            assert r.source == "fake"
            assert len(r.content_hash) == 64

    async def test_marks_task_done_on_success(self, session: AsyncSession) -> None:
        job = await _make_job(session)
        await _queue_task(session, job.id, source="fake")
        task = await claim_one(session)
        assert task is not None

        registry: SourceRegistry = {"fake": FakeSource([_record("ext-1")])}
        await run_one(session, registry, task)
        await session.refresh(task)

        assert task.status == TaskStatus.done
        assert task.completed_at is not None
        assert task.last_error is None

    async def test_deduplicates_by_natural_key_silently(self, session: AsyncSession) -> None:
        """The (source, external_id) UNIQUE constraint quietly absorbs dupes."""
        job = await _make_job(session)
        await _queue_task(session, job.id, source="fake")
        task = await claim_one(session)
        assert task is not None

        # Same external_id returned twice in the same batch.
        registry: SourceRegistry = {"fake": FakeSource([_record("dup"), _record("dup")])}
        await run_one(session, registry, task)

        result = await session.exec(select(RawRecordRow))
        rows = list(result.all())
        assert len(rows) == 1

    async def test_requeues_on_failure_when_attempts_remaining(self, session: AsyncSession) -> None:
        """A failure with attempts < max_attempts goes back to queued."""
        job = await _make_job(session)
        await _queue_task(session, job.id, source="fake", max_attempts=3)
        task = await claim_one(session)
        assert task is not None
        assert task.attempts == 1  # incremented on claim

        registry: SourceRegistry = {"fake": FailingSource()}
        await run_one(session, registry, task)
        await session.refresh(task)

        assert task.status == TaskStatus.queued
        assert task.claimed_at is None
        assert task.last_error is not None

    async def test_marks_failed_when_attempts_exhausted(self, session: AsyncSession) -> None:
        """A failure with attempts == max_attempts goes to failed."""
        job = await _make_job(session)
        # Pre-set attempts so the claim-side increment hits max.
        await _queue_task(session, job.id, source="fake", max_attempts=3, attempts=2)
        task = await claim_one(session)
        assert task is not None
        assert task.attempts == 3  # incremented on claim → at max

        registry: SourceRegistry = {"fake": FailingSource()}
        await run_one(session, registry, task)
        await session.refresh(task)

        assert task.status == TaskStatus.failed
        assert task.last_error is not None
        assert task.completed_at is not None

    async def test_fails_on_unknown_source(self, session: AsyncSession) -> None:
        """An unregistered source name is a permanent failure (no retry)."""
        job = await _make_job(session)
        await _queue_task(session, job.id, source="nonexistent", max_attempts=3, attempts=0)
        task = await claim_one(session)
        assert task is not None

        registry: SourceRegistry = {}  # nothing registered
        await run_one(session, registry, task)
        await session.refresh(task)

        assert task.status == TaskStatus.failed
        assert task.last_error is not None
        assert "nonexistent" in task.last_error


# --- run_worker_once --------------------------------------------------------


class TestRunWorkerOnce:
    async def test_returns_none_when_queue_empty(self, session: AsyncSession) -> None:
        registry: SourceRegistry = {}
        assert await run_worker_once(session, registry) is None

    async def test_processes_one_task_end_to_end(self, session: AsyncSession) -> None:
        """Claim + dispatch + persist + mark done, in one call."""
        job = await _make_job(session)
        await _queue_task(session, job.id, source="fake")

        registry: SourceRegistry = {"fake": FakeSource([_record("e1")])}
        task_id = await run_worker_once(session, registry)

        assert task_id is not None

        task = await session.get(Task, task_id)
        assert task is not None
        assert task.status == TaskStatus.done

        result = await session.exec(select(RawRecordRow))
        rows = list(result.all())
        assert len(rows) == 1
        assert rows[0].external_id == "e1"


# --- sweep_stuck_tasks ------------------------------------------------------


class TestSweepStuckTasks:
    async def test_requeues_idle_running_task(self, session: AsyncSession) -> None:
        """A task running >10min with attempts < max_attempts goes back to queued."""

        job = await _make_job(session)
        task = await _queue_task(session, job.id, source="fake", max_attempts=3, attempts=1)
        # Manually mark as running 15 minutes ago.
        task.status = TaskStatus.running
        task.claimed_at = datetime.now(UTC) - timedelta(minutes=15)
        await session.commit()

        n = await sweep_stuck_tasks(session, idle_minutes=10)

        await session.refresh(task)
        assert n == 1
        assert task.status == TaskStatus.queued
        assert task.claimed_at is None

    async def test_fails_idle_task_at_max_attempts(self, session: AsyncSession) -> None:
        """A stuck task that already used its retries is marked failed."""

        job = await _make_job(session)
        task = await _queue_task(session, job.id, source="fake", max_attempts=3, attempts=3)
        task.status = TaskStatus.running
        task.claimed_at = datetime.now(UTC) - timedelta(minutes=15)
        await session.commit()

        await sweep_stuck_tasks(session, idle_minutes=10)

        await session.refresh(task)
        assert task.status == TaskStatus.failed
        assert task.last_error is not None

    async def test_leaves_fresh_running_tasks_alone(self, session: AsyncSession) -> None:
        """A task running for <10min is not touched."""

        job = await _make_job(session)
        task = await _queue_task(session, job.id, source="fake")
        task.status = TaskStatus.running
        task.claimed_at = datetime.now(UTC) - timedelta(minutes=2)
        await session.commit()

        n = await sweep_stuck_tasks(session, idle_minutes=10)

        await session.refresh(task)
        assert n == 0
        assert task.status == TaskStatus.running


# --- aclose_registry --------------------------------------------------------


class TestAcloseRegistry:
    async def test_calls_aclose_on_each_adapter(self) -> None:
        """Every adapter's aclose() is awaited (releases owned HTTP clients)."""
        closed: list[str] = []

        class ClosableSource(BaseSource):
            name = "fake"
            rate_limit = (1000, 60)

            def __init__(self, tag: str) -> None:
                self._tag = tag

            async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
                return []

            async def aclose(self) -> None:
                closed.append(self._tag)

        registry: SourceRegistry = {"a": ClosableSource("a"), "b": ClosableSource("b")}
        await aclose_registry(registry)
        assert sorted(closed) == ["a", "b"]

    async def test_default_base_aclose_is_safe_noop(self) -> None:
        """A source that does not override aclose is still closeable via the
        BaseSource default (must not raise)."""
        registry: SourceRegistry = {"fake": FakeSource([])}
        await aclose_registry(registry)


# --- run_worker_drain -------------------------------------------------------


class TestRunWorkerDrain:
    async def test_drains_all_queued_tasks(self, session: AsyncSession) -> None:
        """Processes every queued task and returns the count."""
        job = await _make_job(session)
        await _queue_task(session, job.id, source="fake", content_hash="a" * 64)
        await _queue_task(session, job.id, source="fake", content_hash="b" * 64)
        await _queue_task(session, job.id, source="fake", content_hash="c" * 64)

        registry: SourceRegistry = {"fake": FakeSource([_record("e1")])}
        n = await run_worker_drain(session, registry)

        assert n == 3
        assert await run_worker_once(session, registry) is None  # queue drained

    async def test_empty_queue_returns_zero(self, session: AsyncSession) -> None:
        registry: SourceRegistry = {}
        assert await run_worker_drain(session, registry) == 0
