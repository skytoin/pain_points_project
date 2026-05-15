"""Tests for `discovery.llm.stations.query_expansion.run_query_expansion`.

We never call the real OpenAI here. Either:
  - the diskcache is pre-populated with a known JobPlan (cache-hit path)
  - the `call_openai` function is monkeypatched to return a stub
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from diskcache import Cache

from discovery.jobs import JobSpec
from discovery.llm.cache import cache_key, make_cache, put_cached
from discovery.llm.prompts import query_expansion as qe
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.llm.stations import query_expansion as station
from discovery.llm.stations.query_expansion import (
    MODEL,
    QueryExpansionError,
    run_query_expansion,
)


def _valid_query(label: str = "x") -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint="site_wide",
        q=f'(subreddit:startups OR subreddit:smallbusiness) AND "{label}"',
        rationale=label,
    )


def _valid_plan() -> JobPlan:
    return JobPlan(reddit_queries=[_valid_query(f"q{i}") for i in range(10)])


@pytest.fixture
def tmp_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cache]:
    """Point the station's cache at a temp dir for each test."""
    cache = make_cache(tmp_path / "cache")
    monkeypatch.setattr(station, "_cache", cache)
    yield cache
    cache.close()


@pytest.fixture
def spec() -> JobSpec:
    return JobSpec(industry="commercial cleaning", as_of=date(2026, 6, 1))


class TestCacheHit:
    async def test_returns_cached_without_calling_llm(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plan = _valid_plan()
        key = cache_key(
            spec=spec.model_dump(mode="json"),
            prompt_version=qe.VERSION,
            model=MODEL,
        )
        put_cached(tmp_cache, key, plan)

        async def _explode(**kwargs: Any) -> None:
            raise AssertionError("LLM should not be called on cache hit")

        monkeypatch.setattr(station, "call_openai", _explode)

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)
        assert len(result.reddit_queries) == 10


class TestCacheMiss:
    async def test_calls_llm_and_caches_result(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            captured.update(kwargs)
            return _valid_plan()

        monkeypatch.setattr(station, "call_openai", _stub_llm)

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)
        assert captured["model"] == MODEL

        async def _explode(**kwargs: Any) -> None:
            raise AssertionError("expected cache hit on second call")

        monkeypatch.setattr(station, "call_openai", _explode)
        again = await run_query_expansion(spec)
        assert len(again.reddit_queries) == len(result.reddit_queries)


class TestValidationDropsInvalidQueries:
    async def test_drops_lowercase_or_query(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = [_valid_query(f"g{i}") for i in range(10)]
        bad = RedditQuerySpec(
            endpoint="site_wide",
            q='(subreddit:a or subreddit:b) AND "x"',  # lowercase or
            rationale="bad",
        )
        plan = JobPlan(reddit_queries=[*good, bad])

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return plan

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        result = await run_query_expansion(spec)
        assert len(result.reddit_queries) == 10  # bad one dropped


class TestFallbackOnTooFewValidQueries:
    async def test_raises_when_below_min_after_validation(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 9 valid + 6 invalid = 15 (max). Validator drops 6 -> 9 survive,
        # which is below the floor of 10 -> raise QueryExpansionError.
        good = [_valid_query(f"g{i}") for i in range(9)]
        bad = RedditQuerySpec(
            endpoint="site_wide",
            q='(subreddit:a or subreddit:b) AND "x"',
            rationale="bad",
        )
        plan = JobPlan(reddit_queries=[*good, *[bad] * 6])

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return plan

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_raises_when_llm_itself_fails(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _stub_llm(**kwargs: Any) -> JobPlan:
            raise RuntimeError("simulated upstream failure")

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)


class TestTimeWindowOverride:
    """Skill item 11: every query's `t` field is forced to match the
    spec's chosen `time_window`. Belt-and-suspenders — the LLM is told
    in the prompt, but we override too in case it slips.
    """

    async def test_forces_year_on_every_query(
        self,
        tmp_cache: Cache,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        year_spec = JobSpec(
            industry="x", as_of=date(2026, 6, 1), time_window="year"
        )
        # LLM returns queries with mixed `t` values — we should clobber them.
        mixed = [
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups) AND "q{i}"',
                rationale=f"r{i}",
                t=("month" if i % 2 else "week"),  # type: ignore[arg-type]
            )
            for i in range(10)
        ]

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return JobPlan(reddit_queries=mixed)

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        result = await run_query_expansion(year_spec)
        assert {q.t for q in result.reddit_queries} == {"year"}


class TestBaselineSubredditMerge:
    """Skill item 9: always merge LLM-picked subs with the profile-agnostic
    baseline (`r/startups`, `r/microsaas`, `r/smallbusiness`). Defense in
    depth — even if the LLM picks bad subs the baseline keeps the adapter
    useful.
    """

    async def test_merges_baseline_subs_into_plan(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # LLM picks only domain-specific subs; baseline should be added.
        plan = JobPlan(
            reddit_queries=[_valid_query(f"q{i}") for i in range(10)],
            reddit_subreddits=["doggrooming", "groomers", "petbusiness"],
        )

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return plan

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        result = await run_query_expansion(spec)

        for baseline in ("startups", "microsaas", "smallbusiness"):
            assert baseline in result.reddit_subreddits, (
                f"missing baseline sub {baseline!r}"
            )
        # LLM picks come first, baselines appended after
        assert result.reddit_subreddits[:3] == [
            "doggrooming",
            "groomers",
            "petbusiness",
        ]

    async def test_does_not_duplicate_baseline_when_llm_already_picked_it(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the LLM already picked `smallbusiness`, it should appear once."""
        plan = JobPlan(
            reddit_queries=[_valid_query(f"q{i}") for i in range(10)],
            reddit_subreddits=["smallbusiness", "Entrepreneur", "wholesale"],
        )

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return plan

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        result = await run_query_expansion(spec)
        # Count occurrences of smallbusiness — should be exactly 1
        assert result.reddit_subreddits.count("smallbusiness") == 1
        # Other baselines should still be appended
        assert "startups" in result.reddit_subreddits
        assert "microsaas" in result.reddit_subreddits
