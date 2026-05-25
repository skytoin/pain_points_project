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
    _time_window_rfc3339,
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
    """25 valid RedditQuerySpec to satisfy JobPlan's 25-30 band."""
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
