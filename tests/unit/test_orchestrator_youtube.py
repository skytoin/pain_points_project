from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 -- registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, Task  # noqa: F401 -- registers tasks table on metadata
from discovery.jobs import JobSpec
from discovery.llm.schemas import JobPlan, RedditQuerySpec, YouTubeQuerySpec
from discovery.orchestrator.youtube import (
    MAX_YT_QUERIES,
    _compile_yt_queries,
    _queries_from_job_plan,
    _time_window_rfc3339,
    enqueue_youtube_task_for_job,
    youtube_queries_for_spec,
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_reddit_queries(n: int = 25) -> list[RedditQuerySpec]:
    """n valid RedditQuerySpec (default 25) to satisfy JobPlan's 25-30 band."""
    return [
        RedditQuerySpec(
            endpoint="site_wide",
            q=f'(subreddit:startups) AND "test{i}"',
            sort="top",
            t="month",
            limit=100,
            rationale="test",
        )
        for i in range(n)
    ]


def _make_job(
    *,
    industry: str = "cleaning",
    time_window: str = "month",
    job_plan: dict[str, Any] | None = None,
) -> Job:
    spec = JobSpec(
        industry=industry,
        as_of=date(2026, 5, 20),
        time_window=time_window,  # type: ignore[arg-type]
    )
    return Job(
        spec=spec.model_dump(mode="json"),
        spec_hash="testhash",
        job_plan=job_plan,
    )


def _yt(query: str, intent: str = "complaint") -> YouTubeQuerySpec:
    return YouTubeQuerySpec(query=query, intent=intent, rationale="r")  # type: ignore[arg-type]


def _spec(industry: str = "cleaning", time_window: str = "month") -> JobSpec:
    return JobSpec(industry=industry, as_of=date(2026, 5, 22), time_window=time_window)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Task 3.1: TestTimeWindowRfc3339
# ---------------------------------------------------------------------------


class TestTimeWindowRfc3339:
    def test_day_window(self) -> None:
        assert _time_window_rfc3339("day", date(2026, 5, 22)) == "2026-05-21T00:00:00Z"

    def test_hour_window(self) -> None:
        assert _time_window_rfc3339("hour", date(2026, 5, 22)) == "2026-05-21T23:00:00Z"

    def test_week_window(self) -> None:
        assert _time_window_rfc3339("week", date(2026, 5, 22)) == "2026-05-15T00:00:00Z"

    def test_month_window_30_days(self) -> None:
        assert _time_window_rfc3339("month", date(2026, 5, 22)) == "2026-04-22T00:00:00Z"

    def test_year_window_365_days(self) -> None:
        assert _time_window_rfc3339("year", date(2026, 5, 22)) == "2025-05-22T00:00:00Z"

    def test_all_returns_none(self) -> None:
        assert _time_window_rfc3339("all", date(2026, 5, 22)) is None

    def test_unknown_window_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown time window"):
            _time_window_rfc3339("decade", date(2026, 5, 22))


# ---------------------------------------------------------------------------
# Task 3.2: TestCompileYtQueries
# ---------------------------------------------------------------------------


class TestCompileYtQueries:
    def test_normalizes_and_strips(self) -> None:
        out = _compile_yt_queries([_yt("  why  I quit  cleaning ")], _spec())
        assert out[0]["query"] == "why I quit cleaning"

    def test_dedups_case_insensitively(self) -> None:
        out = _compile_yt_queries([_yt("Why I Quit"), _yt("why i quit")], _spec())
        assert len(out) == 1

    def test_caps_at_max_preserving_order(self) -> None:
        out = _compile_yt_queries([_yt(f"q{i} x") for i in range(20)], _spec())
        assert len(out) == MAX_YT_QUERIES == 10
        assert out[0]["query"] == "q0 x"
        assert out[9]["query"] == "q9 x"

    def test_published_after_present_for_month(self) -> None:
        out = _compile_yt_queries([_yt("x")], _spec(time_window="month"))
        assert out[0]["published_after"] == "2026-04-22T00:00:00Z"

    def test_published_after_none_for_all(self) -> None:
        out = _compile_yt_queries([_yt("x")], _spec(time_window="all"))
        assert out[0]["published_after"] is None

    def test_constant_fields(self) -> None:
        out = _compile_yt_queries([_yt("x")], _spec())
        assert out[0]["order"] == "relevance"
        assert out[0]["type"] == "video"
        assert out[0]["part"] == "snippet"
        assert out[0]["max_results"] == 50

    def test_empty_specs_returns_empty_list(self) -> None:
        assert _compile_yt_queries([], _spec()) == []

    def test_drops_whitespace_only_query(self) -> None:
        # "  " passes YouTubeQuerySpec min_length=1 but normalizes to empty.
        out = _compile_yt_queries([_yt("  "), _yt("why I quit")], _spec())
        assert len(out) == 1
        assert out[0]["query"] == "why I quit"


# ---------------------------------------------------------------------------
# Task 3.3: TestYoutubeQueriesForSpec
# ---------------------------------------------------------------------------


class TestYoutubeQueriesForSpec:
    def test_returns_compiled_queries(self) -> None:
        out = youtube_queries_for_spec(_spec(industry="cleaning"))
        assert 1 <= len(out) <= MAX_YT_QUERIES
        assert any("quit" in q["query"] for q in out)

    def test_each_carries_published_after(self) -> None:
        out = youtube_queries_for_spec(_spec(industry="cleaning", time_window="year"))
        assert all(q["published_after"] == "2025-05-22T00:00:00Z" for q in out)

    def test_deterministic(self) -> None:
        s = _spec(industry="cleaning")
        assert youtube_queries_for_spec(s) == youtube_queries_for_spec(s)


# ---------------------------------------------------------------------------
# Task 3.4: TestQueriesFromJobPlan + TestEnqueueYoutubeTaskForJob
# ---------------------------------------------------------------------------


class TestQueriesFromJobPlan:
    def test_returns_none_when_job_plan_is_null(self) -> None:
        job = _make_job(job_plan=None)
        assert _queries_from_job_plan(job) is None

    def test_returns_empty_list_when_youtube_queries_is_empty(self) -> None:
        """Permissive default: LLM intentionally emitted [] (graceful
        sparsity). Return [] -- caller does NOT fall back to template."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), youtube_queries=[])
        job = _make_job(job_plan=plan.model_dump())
        assert _queries_from_job_plan(job) == []

    def test_compiles_when_youtube_queries_present(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            youtube_queries=[
                YouTubeQuerySpec(
                    query="why I quit cleaning",
                    intent="complaint",
                    rationale="r",
                ),
            ],
        )
        job = _make_job(job_plan=plan.model_dump())
        out = _queries_from_job_plan(job)
        assert out is not None
        assert len(out) == 1
        assert out[0]["query"] == "why I quit cleaning"

    def test_returns_none_on_validation_failure(self) -> None:
        """If `job_plan` is set but its shape doesn't validate, fall back
        to template (None signal) -- defensive against schema drift."""
        job = _make_job(job_plan={"reddit_queries": "wrong shape"})
        assert _queries_from_job_plan(job) is None


class TestEnqueueYoutubeTaskForJob:
    async def test_creates_task_with_compiled_queries_when_plan_present(
        self, session: AsyncSession
    ) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            youtube_queries=[
                YouTubeQuerySpec(
                    query="why I quit cleaning",
                    intent="complaint",
                    rationale="r",
                ),
            ],
        )
        job = _make_job(job_plan=plan.model_dump())
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_youtube_task_for_job(session, job)

        assert task.id is not None
        assert task.job_id == job.id
        assert task.source == "youtube"
        assert task.action == "fetch"
        assert len(task.params["queries"]) == 1
        assert task.params["queries"][0]["query"] == "why I quit cleaning"

    async def test_falls_back_to_template_when_job_plan_null(self, session: AsyncSession) -> None:
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_youtube_task_for_job(session, job)

        # Template emits 5 candidates; compile pipeline keeps all 5.
        assert len(task.params["queries"]) == 5
        assert any("quit" in q["query"] for q in task.params["queries"])

    async def test_creates_task_even_when_youtube_queries_intentionally_empty(
        self, session: AsyncSession
    ) -> None:
        """Empty `youtube_queries` (LLM said 'no YouTube signal') creates
        a no-op task -- graceful sparsity."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), youtube_queries=[])
        job = _make_job(job_plan=plan.model_dump())
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_youtube_task_for_job(session, job)

        assert task.params["queries"] == []

    async def test_idempotent_on_content_hash(self, session: AsyncSession) -> None:
        """Re-enqueuing the same job returns the existing task."""
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task_a = await enqueue_youtube_task_for_job(session, job)
        task_b = await enqueue_youtube_task_for_job(session, job)

        assert task_a.id == task_b.id

    async def test_template_path_sets_wave_and_action(self, session: AsyncSession) -> None:
        """The template (null job_plan) path also stamps wave=1 / action=fetch
        (the compiled-plan path asserts action; this covers wave on both)."""
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_youtube_task_for_job(session, job)

        assert task.action == "fetch"
        assert task.wave == 1
