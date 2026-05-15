"""Tests for `discovery.orchestrator.reddit` — the hand-rolled Wave 1
query template + the per-job task enqueue function.

Wave 0 (LLM query expansion) will replace the template later; until then
these tests pin the deterministic behavior callers can rely on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 — registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Task
from discovery.jobs import JobSpec, create_job
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.orchestrator.reddit import (
    enqueue_reddit_task_for_job,
    reddit_queries_for_spec,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()


# --- reddit_queries_for_spec ------------------------------------------------


class TestRedditQueriesForSpec:
    def test_returns_at_least_one_query(self) -> None:
        spec = JobSpec(industry="commercial cleaning", as_of=date(2026, 6, 1))
        queries = reddit_queries_for_spec(spec)
        assert len(queries) >= 1

    def test_each_query_has_the_required_keys(self) -> None:
        """Every query must be a valid input shape for `RedditSource.fetch`."""
        spec = JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        for q in reddit_queries_for_spec(spec):
            assert q["endpoint"] in ("per_sub", "site_wide")
            assert isinstance(q["q"], str)
            assert q["q"]
            assert q["sort"] in ("top", "hot", "new")
            assert q["t"] in ("hour", "day", "week", "month", "year", "all")
            assert isinstance(q["limit"], int)
            assert 0 < q["limit"] <= 100

    def test_queries_mention_the_industry_as_a_quoted_phrase(self) -> None:
        """Quoted industry literal narrows results to posts that name it."""
        spec = JobSpec(industry="commercial cleaning", as_of=date(2026, 6, 1))
        for q in reddit_queries_for_spec(spec):
            assert '"commercial cleaning"' in q["q"]

    def test_url_length_budget_stays_under_4kb(self) -> None:
        """Per `reddit-source` skill item 7: Reddit's URL ceiling is ~4 KB."""
        spec = JobSpec(industry="x" * 100, as_of=date(2026, 6, 1))
        for q in reddit_queries_for_spec(spec):
            assert len(q["q"]) < 4000

    def test_each_query_includes_a_pain_phrase_clause(self) -> None:
        """Per skill item 8: phrases need variants OR'd together, not bare keywords."""
        spec = JobSpec(industry="x", as_of=date(2026, 6, 1))
        for q in reddit_queries_for_spec(spec):
            assert " OR " in q["q"]  # phrase OR phrase ... clause


# --- enqueue_reddit_task_for_job -------------------------------------------


class TestEnqueueRedditTaskForJob:
    async def test_creates_one_reddit_task(self, session: AsyncSession) -> None:
        job = await create_job(session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1)))
        task = await enqueue_reddit_task_for_job(session, job)

        assert task.id is not None
        assert task.source == "reddit"
        assert task.wave == 1
        assert task.job_id == job.id
        assert isinstance(task.params, dict)
        assert "queries" in task.params
        assert len(task.params["queries"]) >= 1

    async def test_idempotent_on_same_job(self, session: AsyncSession) -> None:
        """Re-enqueueing the same job returns the same task — never duplicates."""
        job = await create_job(session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1)))
        first = await enqueue_reddit_task_for_job(session, job)
        second = await enqueue_reddit_task_for_job(session, job)

        assert first.id == second.id

        result = await session.exec(select(Task).where(Task.job_id == job.id))
        rows = list(result.all())
        assert len(rows) == 1

    async def test_different_jobs_get_independent_tasks(self, session: AsyncSession) -> None:
        """The same query template applied to a different industry → different task."""
        cleaning = await create_job(session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1)))
        bakery = await create_job(session, JobSpec(industry="bakery", as_of=date(2026, 6, 1)))
        t1 = await enqueue_reddit_task_for_job(session, cleaning)
        t2 = await enqueue_reddit_task_for_job(session, bakery)

        assert t1.id != t2.id
        assert t1.content_hash != t2.content_hash


# --- new: reads from job.job_plan when populated -----------------------------


class TestReadsFromJobPlan:
    async def test_uses_job_plan_queries_when_present(self, session: AsyncSession) -> None:
        """When job.job_plan is populated, the orchestrator uses those queries
        verbatim — it does not fall through to the hand-rolled template."""
        job = await create_job(session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1)))
        llm_queries = [
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups) AND "llm{i}"',
                rationale="x",
            )
            for i in range(10)
        ]
        job.job_plan = JobPlan(reddit_queries=llm_queries).model_dump()
        session.add(job)
        await session.commit()

        task = await enqueue_reddit_task_for_job(session, job)
        assert len(task.params["queries"]) == 10
        for q in task.params["queries"]:
            assert '"llm' in q["q"]

    async def test_falls_back_to_template_when_job_plan_null(self, session: AsyncSession) -> None:
        """No job_plan → use the existing hand-rolled template."""
        job = await create_job(session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1)))
        assert job.job_plan is None
        task = await enqueue_reddit_task_for_job(session, job)
        assert "queries" in task.params
        # Template queries all include the quoted industry literal.
        assert all('"cleaning"' in q["q"] for q in task.params["queries"])
