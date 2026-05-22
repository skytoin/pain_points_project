from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 -- registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, Task  # noqa: F401 -- registers tasks table on metadata
from discovery.jobs import JobSpec
from discovery.llm.schemas import HackerNewsKeywordSpec, JobPlan, RedditQuerySpec
from discovery.orchestrator.hackernews import (
    MAX_HN_QUERIES,
    _compile_hn_queries,
    _queries_from_job_plan,
    _routing_for,
    _time_window_epoch,
    enqueue_hn_task_for_job,
    hn_keyword_candidates_for_spec,
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


def _epoch(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp())


class TestTimeWindowEpoch:
    def test_hour_window(self) -> None:
        anchor = date(2026, 5, 20)
        # Anchor at 2026-05-20 00:00 UTC; subtract 1 hour -> 2026-05-19 23:00 UTC.
        assert _time_window_epoch("hour", anchor) == _epoch(2026, 5, 19, 23)

    def test_day_window(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("day", anchor) == _epoch(2026, 5, 19)

    def test_week_window(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("week", anchor) == _epoch(2026, 5, 13)

    def test_month_window_30_days(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("month", anchor) == _epoch(2026, 4, 20)

    def test_year_window_365_days(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("year", anchor) == _epoch(2025, 5, 20)

    def test_all_returns_none(self) -> None:
        """`all` -> None signals 'omit created_at_i entirely from numericFilters'."""
        assert _time_window_epoch("all", date(2026, 5, 20)) is None

    def test_unknown_window_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown time window"):
            _time_window_epoch("decade", date(2026, 5, 20))


class TestRoutingFor:
    def test_launch_routes_to_search_by_date_show_hn_relaxed(self) -> None:
        endpoint, tags, extra = _routing_for("launch")
        assert endpoint == "search_by_date"
        assert tags == "show_hn"
        assert extra == []  # no points/comments floor -- recency is the signal

    def test_context_routes_to_search_story_with_quality_floor(self) -> None:
        endpoint, tags, extra = _routing_for("context")
        assert endpoint == "search"
        assert tags == "story"
        assert extra == ["points>5", "num_comments>3"]

    def test_unknown_intent_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            _routing_for("unknown")


def _kw(keyword: str, intent: str = "launch") -> HackerNewsKeywordSpec:
    return HackerNewsKeywordSpec(
        keyword=keyword,
        intent=intent,  # type: ignore[arg-type]
        rationale="test",
    )


def _spec(industry: str = "test industry", time_window: str = "month") -> JobSpec:
    return JobSpec(
        industry=industry,
        as_of=date(2026, 5, 20),
        time_window=time_window,  # type: ignore[arg-type]
    )


class TestCompileHnQueries:
    def test_decomposes_each_keyword_to_two_tokens(self) -> None:
        out = _compile_hn_queries([_kw("Personal CRM local-first")], _spec())
        assert len(out) == 1
        # "local-first" at position 3 is dropped by decompose_keyword.
        assert out[0]["query"] == "Personal CRM"

    def test_drops_empty_decomposition(self) -> None:
        # All-stopwords keyword decomposes to [] -> dropped silently.
        out = _compile_hn_queries([_kw("the a an")], _spec())
        assert out == []

    def test_dedupes_on_token_tuple(self) -> None:
        # Same keyword twice -> compiled once.
        out = _compile_hn_queries([_kw("MCP server"), _kw("MCP server")], _spec())
        assert len(out) == 1

    def test_dedup_is_case_sensitive(self) -> None:
        # `MCP` and `mcp` are different on HN (acronym casing matters).
        out = _compile_hn_queries([_kw("MCP server"), _kw("mcp server")], _spec())
        assert len(out) == 2

    def test_routes_launch_to_search_by_date_show_hn(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI", intent="launch")], _spec())
        assert out[0]["endpoint"] == "search_by_date"
        assert out[0]["tags"] == "show_hn"

    def test_routes_context_to_search_with_quality_floor(self) -> None:
        out = _compile_hn_queries([_kw("CRM founder", intent="context")], _spec())
        assert out[0]["endpoint"] == "search"
        assert out[0]["tags"] == "story"
        assert "points>5" in out[0]["numeric_filters"]
        assert "num_comments>3" in out[0]["numeric_filters"]

    def test_includes_created_at_i_when_time_window_is_not_all(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI")], _spec(time_window="month"))
        assert "created_at_i>" in out[0]["numeric_filters"]

    def test_omits_all_filters_for_launch_plus_all_time_window(self) -> None:
        """Launch intent + time_window='all' -> no quality floor and no
        recency floor -> numeric_filters is the empty string."""
        out = _compile_hn_queries([_kw("CRM CLI", intent="launch")], _spec(time_window="all"))
        assert out[0]["numeric_filters"] == ""

    def test_context_with_all_time_keeps_quality_floor(self) -> None:
        out = _compile_hn_queries([_kw("CRM founder", intent="context")], _spec(time_window="all"))
        # No created_at_i but points/num_comments still apply.
        assert "created_at_i" not in out[0]["numeric_filters"]
        assert "points>5" in out[0]["numeric_filters"]
        assert "num_comments>3" in out[0]["numeric_filters"]

    def test_caps_at_max_hn_queries_preserving_llm_order(self) -> None:
        kws = [_kw(f"kw{i} tok") for i in range(20)]
        out = _compile_hn_queries(kws, _spec())
        assert len(out) == MAX_HN_QUERIES == 12
        # Order preserved -- LLM ranking signal (spec §8).
        assert out[0]["query"] == "kw0 tok"
        assert out[11]["query"] == "kw11 tok"

    def test_hits_per_page_is_30(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI")], _spec())
        assert out[0]["hits_per_page"] == 30

    def test_query_is_space_joined_tokens(self) -> None:
        # Tokens joined by single space -- HN treats whitespace as token-AND.
        out = _compile_hn_queries([_kw("CRM CLI")], _spec())
        assert out[0]["query"] == "CRM CLI"

    def test_created_at_i_appears_first_in_filter_string(self) -> None:
        out = _compile_hn_queries(
            [_kw("CRM founder", intent="context")], _spec(time_window="month")
        )
        # Convention: time filter first, quality filters after.
        filters = out[0]["numeric_filters"]
        assert filters.startswith("created_at_i>")


class TestHnKeywordCandidatesForSpec:
    def test_returns_at_least_one_compiled_query(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning"))
        # Template has 4 candidates -- all should compile cleanly for a
        # single-word industry.
        assert len(out) >= 1

    def test_capability_first_survives_decomposition_for_multiword_industry(self) -> None:
        """For 'commercial cleaning', the template uses `CLI commercial
        cleaning` etc. so the capability word lands in position 1 and
        survives the 2-token cap."""
        out = hn_keyword_candidates_for_spec(_spec(industry="commercial cleaning"))
        queries = [q["query"] for q in out]
        # Every query starts with a capability word (CLI/OSS/API/workflow)
        # followed by the first industry word.
        assert any(q.startswith("CLI ") for q in queries)
        assert any(q.startswith("OSS ") for q in queries)
        assert any(q.startswith("API ") for q in queries)
        assert any(q.startswith("workflow ") for q in queries)

    def test_includes_both_launch_and_context_queries(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning"))
        endpoints = {q["endpoint"] for q in out}
        # CLI/OSS/API are launch, workflow is context.
        assert "search_by_date" in endpoints  # at least one launch
        assert "search" in endpoints  # at least one context

    def test_each_query_carries_the_time_window_filter(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning", time_window="year"))
        for q in out:
            # year window -> created_at_i floor present on every query.
            assert "created_at_i>" in q["numeric_filters"]

    def test_template_output_is_deterministic(self) -> None:
        """Same spec, same compiled output -- the template + compile
        pipeline are pure. (Dedup behavior for collision cases is
        exercised separately in TestCompileHnQueries.)"""
        spec = _spec(industry="cleaning")
        assert hn_keyword_candidates_for_spec(spec) == hn_keyword_candidates_for_spec(spec)


# ---------------------------------------------------------------------------
# Helpers shared by TestQueriesFromJobPlan and TestEnqueueHnTaskForJob
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


class TestQueriesFromJobPlan:
    def test_returns_none_when_job_plan_is_null(self) -> None:
        job = _make_job(job_plan=None)
        assert _queries_from_job_plan(job) is None

    def test_returns_empty_list_when_hn_queries_is_empty(self) -> None:
        """Permissive default: LLM intentionally emitted [] (graceful
        sparsity per §8 / §17). Return [] -- caller does NOT fall back
        to template, because the LLM deliberately said nothing."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), hn_queries=[])
        job = _make_job(job_plan=plan.model_dump())
        assert _queries_from_job_plan(job) == []

    def test_compiles_when_hn_queries_present(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(
                    keyword="CRM CLI",
                    intent="launch",
                    rationale="r",
                ),
            ],
        )
        job = _make_job(job_plan=plan.model_dump())
        out = _queries_from_job_plan(job)
        assert out is not None
        assert len(out) == 1
        assert out[0]["tags"] == "show_hn"

    def test_returns_none_on_validation_failure(self) -> None:
        """If `job_plan` is set but its shape doesn't validate, fall back
        to template (None signal) -- defensive against schema drift."""
        job = _make_job(job_plan={"reddit_queries": "wrong shape"})
        assert _queries_from_job_plan(job) is None


class TestEnqueueHnTaskForJob:
    async def test_creates_task_with_compiled_queries_when_plan_present(
        self, session: AsyncSession
    ) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(
                    keyword="CRM CLI",
                    intent="launch",
                    rationale="r",
                ),
            ],
        )
        job = _make_job(job_plan=plan.model_dump())
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_hn_task_for_job(session, job)

        assert task.id is not None
        assert task.job_id == job.id
        assert task.source == "hackernews"
        assert task.action == "fetch"
        assert len(task.params["queries"]) == 1
        assert task.params["queries"][0]["tags"] == "show_hn"

    async def test_falls_back_to_template_when_job_plan_null(self, session: AsyncSession) -> None:
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_hn_task_for_job(session, job)

        # Template emits 4 candidates; compile pipeline keeps all 4.
        assert len(task.params["queries"]) == 4
        endpoints = {q["endpoint"] for q in task.params["queries"]}
        assert "search_by_date" in endpoints
        assert "search" in endpoints

    async def test_creates_task_even_when_hn_queries_intentionally_empty(
        self, session: AsyncSession
    ) -> None:
        """Empty `hn_queries` (LLM said 'this industry has no HN signal')
        creates a no-op task -- graceful sparsity. The task runs, fetches
        zero records, completes `done`."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), hn_queries=[])
        job = _make_job(job_plan=plan.model_dump())
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_hn_task_for_job(session, job)

        assert task.params["queries"] == []

    async def test_idempotent_on_content_hash(self, session: AsyncSession) -> None:
        """Re-enqueuing the same job returns the existing task (UNIQUE on
        (job_id, content_hash)). No duplicate Bronze fetches."""
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task_a = await enqueue_hn_task_for_job(session, job)
        task_b = await enqueue_hn_task_for_job(session, job)

        assert task_a.id == task_b.id
