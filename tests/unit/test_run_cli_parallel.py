"""Parallel fan-out tests for `discovery.cli.run._run_task_in_own_session`.

The discovery run command enqueues a Reddit task and an HN task per
job, then dispatches both concurrently via `asyncio.gather`. This file
tests the per-id-claim + dispatch helper directly (faster + clearer
than booting the whole CLI) and pins the wall-clock overlap that
proves concurrency.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime  # noqa: F401 -- kept for spec parity
from typing import Any

import pytest
from sqlmodel import SQLModel

from discovery.cli.run import _run_task_in_own_session
from discovery.db import models  # noqa: F401 -- registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, Task, TaskStatus
from discovery.sources.base import BaseSource, RawRecord


class _RecordingBase(BaseSource):
    """Test double that records its start time and sleeps a known
    interval. Used to prove that two adapters run with overlapping
    wall-clock windows when dispatched concurrently.

    `BaseSource.__init_subclass__` requires `name` to be a CLASS
    attribute (not just an instance attribute / annotation), so the
    two concrete doubles below set `name` at class scope. Setting
    `self.name = ...` in `__init__` would NOT satisfy the check --
    the check runs at class creation, before `__init__` is called.
    """

    name = "recording"  # subclasses override; required so __init_subclass__ check passes
    rate_limit = (10, 1)

    def __init__(self, started_at: dict[str, float], sleep_s: float) -> None:
        self._started_at = started_at
        self._sleep_s = sleep_s

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        self._started_at[self.name] = time.monotonic()
        await asyncio.sleep(self._sleep_s)
        return [RawRecord(source=self.name, external_id=f"{self.name}-1", body={})]


class _RedditDouble(_RecordingBase):
    name = "reddit"


class _HNDouble(_RecordingBase):
    name = "hackernews"


class _YouTubeDouble(_RecordingBase):
    name = "youtube"


@pytest.fixture
async def maker() -> AsyncIterator[Any]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_session_factory(engine)
    await engine.dispose()


async def _make_queued_task(maker: Any, source: str) -> int:
    """Insert a job + queued task; return the task id."""
    async with maker() as s:
        job = Job(
            spec={"industry": "x", "as_of": "2026-05-20", "time_window": "month"},
            spec_hash=f"h-{source}",
        )
        s.add(job)
        await s.commit()
        await s.refresh(job)

        task = Task(
            job_id=job.id,
            wave=1,
            source=source,
            action="fetch",
            params={"queries": []},
            content_hash=f"hash-{source}",
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task.id  # type: ignore[return-value]


class TestRunTaskInOwnSession:
    async def test_claims_and_dispatches_known_task(self, maker: Any) -> None:
        task_id = await _make_queued_task(maker, "reddit")
        started: dict[str, float] = {}
        registry: dict[str, BaseSource] = {"reddit": _RedditDouble(started, 0.0)}

        await _run_task_in_own_session(maker, registry, task_id)

        async with maker() as s:
            task = await s.get(Task, task_id)
            assert task is not None
            assert task.status == TaskStatus.done
        assert started["reddit"]

    async def test_returns_silently_when_task_already_claimed(self, maker: Any) -> None:
        """If claim_known_task returns None (task no longer queued),
        the helper logs a warning and returns -- it does NOT raise."""
        task_id = await _make_queued_task(maker, "reddit")
        async with maker() as s:
            task = await s.get(Task, task_id)
            assert task is not None
            task.status = TaskStatus.running
            await s.commit()

        registry: dict[str, BaseSource] = {}
        await _run_task_in_own_session(maker, registry, task_id)


class TestParallelFanout:
    async def test_two_tasks_dispatch_concurrently(self, maker: Any) -> None:
        """Wall-clock overlap proves the two branches actually run in
        parallel via asyncio.gather -- not sequentially."""
        reddit_id = await _make_queued_task(maker, "reddit")
        hn_id = await _make_queued_task(maker, "hackernews")

        started: dict[str, float] = {}
        sleep_s = 0.05  # 50 ms each
        registry: dict[str, BaseSource] = {
            "reddit": _RedditDouble(started, sleep_s),
            "hackernews": _HNDouble(started, sleep_s),
        }

        t0 = time.monotonic()
        await asyncio.gather(
            _run_task_in_own_session(maker, registry, reddit_id),
            _run_task_in_own_session(maker, registry, hn_id),
        )
        wall = time.monotonic() - t0

        # Both started within 30ms of each other -- overlapped. Bound
        # is generous for Windows CI's ~15ms timer granularity.
        assert len(started) == 2
        assert abs(started["reddit"] - started["hackernews"]) < 0.03

        # Total wall-clock is closer to 50ms (max) than 100ms (sum).
        # 90ms upper bound: well under the 100ms sequential floor.
        assert wall < 0.09, f"wall={wall:.3f}s -- looks sequential"

    async def test_three_tasks_dispatch_concurrently(self, maker: Any) -> None:
        """Three-way overlap proves Reddit + HN + YouTube all run in
        parallel via asyncio.gather -- not sequentially."""
        reddit_id = await _make_queued_task(maker, "reddit")
        hn_id = await _make_queued_task(maker, "hackernews")
        yt_id = await _make_queued_task(maker, "youtube")

        started: dict[str, float] = {}
        registry: dict[str, BaseSource] = {
            "reddit": _RedditDouble(started, 0.05),
            "hackernews": _HNDouble(started, 0.05),
            "youtube": _YouTubeDouble(started, 0.05),
        }
        t0 = time.monotonic()
        await asyncio.gather(
            _run_task_in_own_session(maker, registry, reddit_id),
            _run_task_in_own_session(maker, registry, hn_id),
            _run_task_in_own_session(maker, registry, yt_id),
        )
        wall = time.monotonic() - t0
        assert len(started) == 3

        # All three started within 30ms of each other -- overlapped, not
        # staggered sequentially. Mirrors the two-way test's overlap check.
        assert max(started.values()) - min(started.values()) < 0.03

        assert wall < 0.12, f"wall={wall:.3f}s -- looks sequential"
