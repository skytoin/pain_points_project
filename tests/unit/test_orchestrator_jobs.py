"""Tests for `discovery.orchestrator.jobs.plan_job`.

We never call the real LLM - we monkeypatch `run_query_expansion`
inside the orchestrator module. plan_job's contract:

- On success: `job.job_plan` is populated with the JobPlan dict.
- On QueryExpansionError: `job.job_plan` stays null; returns the
  unchanged Job so the caller proceeds to fallback.
- Idempotent: a populated job_plan short-circuits the LLM call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 - registers tables
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.jobs import JobSpec, create_job
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.llm.stations.query_expansion import QueryExpansionError
from discovery.orchestrator import jobs as jobs_module
from discovery.orchestrator.jobs import plan_job


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()


def _valid_plan() -> JobPlan:
    return JobPlan(
        reddit_queries=[
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups OR subreddit:smallbusiness) AND "p{i}"',
                rationale="x",
            )
            for i in range(10)
        ]
    )


class TestPlanJob:
    async def test_populates_job_plan_on_success(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _stub(spec: JobSpec) -> JobPlan:
            return _valid_plan()

        monkeypatch.setattr(jobs_module, "run_query_expansion", _stub)
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        assert job.job_plan is None

        updated = await plan_job(session, job)
        assert updated.job_plan is not None
        assert "reddit_queries" in updated.job_plan
        assert len(updated.job_plan["reddit_queries"]) == 10

    async def test_leaves_job_plan_null_on_failure(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fail(spec: JobSpec) -> JobPlan:
            raise QueryExpansionError("simulated")

        monkeypatch.setattr(jobs_module, "run_query_expansion", _fail)
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        updated = await plan_job(session, job)
        assert updated.job_plan is None

    async def test_idempotent_returns_existing_plan(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If job.job_plan is already populated, don't call the LLM again."""

        async def _explode(spec: JobSpec) -> JobPlan:
            raise AssertionError("station should not be called when plan exists")

        monkeypatch.setattr(jobs_module, "run_query_expansion", _explode)
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        job.job_plan = _valid_plan().model_dump()
        session.add(job)
        await session.commit()

        updated = await plan_job(session, job)
        assert updated.job_plan is not None
