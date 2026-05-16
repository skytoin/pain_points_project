"""Tests for `discovery.view` — pure data-fetching for the inspect CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 — registers tables
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import RawRecordRow
from discovery.hashing import hash_params
from discovery.jobs import JobSpec, create_job
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.view import JobSummary, gather_job_detail, gather_job_summaries


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()


def _plan(n: int = 25) -> JobPlan:
    return JobPlan(
        reddit_queries=[
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups) AND "q{i}"',
                rationale=f"reason {i}",
            )
            for i in range(n)
        ],
        reddit_subreddits=["startups", "smallbusiness"],
    )


async def _insert_post(
    session: AsyncSession,
    job_id: int,
    external_id: str,
    title: str,
    score: int = 42,
) -> None:
    # The Reddit adapter stores the inner `data` dict as `body` directly
    # (it strips the {"kind": "t3", "data": ...} envelope on its way in).
    # So our fields live at the top level, not under "data".
    body = {
        "title": title,
        "subreddit": "foodtrucks",
        "score": score,
        "num_comments": 7,
        "permalink": f"/r/foodtrucks/comments/{external_id}",
        "selftext": "body text",
    }
    row = RawRecordRow(
        job_id=job_id,
        task_id=1,
        source="reddit",
        external_id=external_id,
        body=body,
        content_hash=hash_params(body),
    )
    session.add(row)
    await session.commit()


class TestGatherJobSummaries:
    async def test_empty_db_returns_empty_list(self, session: AsyncSession) -> None:
        assert await gather_job_summaries(session) == []

    async def test_one_job_no_plan_no_posts(self, session: AsyncSession) -> None:
        job = await create_job(session, JobSpec(industry="x", as_of=date(2026, 6, 1)))
        rows = await gather_job_summaries(session)
        assert len(rows) == 1
        s = rows[0]
        assert isinstance(s, JobSummary)
        assert s.id == job.id
        assert s.industry == "x"
        assert s.planned is False
        assert s.post_count == 0

    async def test_one_job_planned_with_posts(self, session: AsyncSession) -> None:
        job = await create_job(session, JobSpec(industry="food truck", as_of=date(2026, 6, 1)))
        job.job_plan = _plan().model_dump()
        session.add(job)
        await session.commit()
        assert job.id is not None
        await _insert_post(session, job.id, "a1", "first")
        await _insert_post(session, job.id, "a2", "second")

        rows = await gather_job_summaries(session)
        assert len(rows) == 1
        assert rows[0].planned is True
        assert rows[0].post_count == 2
        assert rows[0].industry == "food truck"


class TestGatherJobDetail:
    async def test_missing_job_returns_none(self, session: AsyncSession) -> None:
        assert await gather_job_detail(session, job_id=999) is None

    async def test_returns_plan_and_posts(self, session: AsyncSession) -> None:
        job = await create_job(session, JobSpec(industry="food truck", as_of=date(2026, 6, 1)))
        job.job_plan = _plan(n=25).model_dump()
        session.add(job)
        await session.commit()
        assert job.id is not None
        for i in range(3):
            await _insert_post(session, job.id, f"p{i}", f"title {i}")

        detail = await gather_job_detail(session, job_id=job.id)
        assert detail is not None
        assert detail.summary.id == job.id
        assert detail.summary.post_count == 3
        assert detail.plan is not None
        assert len(detail.plan.reddit_queries) == 25
        # posts come back as PostView, capped at the limit
        assert len(detail.posts) == 3
        assert detail.posts[0].title.startswith("title")
        assert detail.posts[0].score == 42
        assert detail.posts[0].permalink.startswith("/r/")

    async def test_post_limit_caps_returned_posts(self, session: AsyncSession) -> None:
        job = await create_job(session, JobSpec(industry="x", as_of=date(2026, 6, 1)))
        assert job.id is not None
        for i in range(20):
            await _insert_post(session, job.id, f"p{i}", f"title {i}", score=i)

        detail = await gather_job_detail(session, job_id=job.id, post_limit=5)
        assert detail is not None
        assert len(detail.posts) == 5

    async def test_handles_job_with_no_plan(self, session: AsyncSession) -> None:
        """Fallback path: a job with no job_plan still shows up with detail.plan = None."""
        job = await create_job(session, JobSpec(industry="x", as_of=date(2026, 6, 1)))
        assert job.id is not None
        detail = await gather_job_detail(session, job_id=job.id)
        assert detail is not None
        assert detail.plan is None
