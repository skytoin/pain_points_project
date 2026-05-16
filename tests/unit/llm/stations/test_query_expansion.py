"""Tests for `run_query_expansion` — the integrated Wave 0 flow.

Never calls real OpenAI or real Reddit. We monkeypatch BOTH:
  - `station.call_openai` — a dispatcher returning SubredditSearchPhrases
    for Call #1 and a JobPlan for Call #2 (keyed on `response_model`).
  - `station.search_subreddits` — a fake returning canned PhraseResults.
Plus the diskcache is pointed at a temp dir per test.
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
from discovery.llm.prompts import subreddit_phrases as sp
from discovery.llm.schemas import JobPlan, RedditQuerySpec, SubredditSearchPhrases
from discovery.llm.stations import query_expansion as station
from discovery.llm.stations.query_expansion import (
    MODEL,
    QueryExpansionError,
    run_query_expansion,
)
from discovery.sources.reddit_subreddits import PhraseResult, SubredditCandidate


def _query(label: str = "x", sub: str = "startups") -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint="site_wide",
        q=f'(subreddit:{sub}) AND "{label}"',
        rationale=label,
    )


def _plan(subs: list[str] | None = None, n: int = 25) -> JobPlan:
    return JobPlan(
        reddit_queries=[_query(f"q{i}") for i in range(n)],
        reddit_subreddits=subs if subs is not None else ["startups"],
    )


def _candidates(*names: str) -> list[SubredditCandidate]:
    return [
        SubredditCandidate(
            name=n,
            subscribers=5000,
            active_user_count=120,
            subreddit_type="public",
            public_description=f"{n} practitioners",
        )
        for n in (names or ("startups",))
    ]


def _make_call_openai(
    plan: JobPlan,
    phrases: SubredditSearchPhrases | None = None,
) -> Any:
    async def _call(**kwargs: Any) -> Any:
        if kwargs["response_model"] is SubredditSearchPhrases:
            return phrases or SubredditSearchPhrases(phrases=["a", "b", "c"])
        return plan

    return _call


def _make_search(*names: str) -> Any:
    async def _search(phrases: list[str], **kwargs: Any) -> list[PhraseResult]:
        return [PhraseResult(phrase=p, candidates=_candidates(*names)) for p in phrases]

    return _search


@pytest.fixture
def tmp_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cache]:
    cache = make_cache(tmp_path / "cache")
    monkeypatch.setattr(station, "_cache", cache)
    yield cache
    cache.close()


@pytest.fixture
def spec() -> JobSpec:
    return JobSpec(industry="commercial cleaning", as_of=date(2026, 6, 1))


def _combined_key(spec: JobSpec) -> str:
    return cache_key(
        spec=spec.model_dump(mode="json"),
        prompt_version=f"{sp.VERSION}+{qe.VERSION}",
        model=MODEL,
    )


class TestCacheHit:
    async def test_cache_hit_skips_both_calls_and_the_client(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        put_cached(tmp_cache, _combined_key(spec), _plan())

        async def _explode_llm(**kwargs: Any) -> None:
            raise AssertionError("no LLM call on cache hit")

        async def _explode_search(*a: Any, **k: Any) -> None:
            raise AssertionError("no sub-search on cache hit")

        monkeypatch.setattr(station, "call_openai", _explode_llm)
        monkeypatch.setattr(station, "search_subreddits", _explode_search)

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)
        assert len(result.reddit_queries) == 25


class TestCacheMiss:
    async def test_runs_full_chain_then_caches(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(station, "call_openai", _make_call_openai(_plan()))
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)

        async def _explode(**kwargs: Any) -> None:
            raise AssertionError("expected cache hit on second call")

        async def _explode_search(*a: Any, **k: Any) -> None:
            raise AssertionError("no sub-search on cache hit")

        monkeypatch.setattr(station, "call_openai", _explode)
        monkeypatch.setattr(station, "search_subreddits", _explode_search)
        again = await run_query_expansion(spec)
        assert len(again.reddit_queries) == len(result.reddit_queries)


class TestValidationDropsInvalidQueries:
    async def test_drops_lowercase_or_query_via_existing_tail(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = [_query(f"g{i}") for i in range(25)]
        bad = RedditQuerySpec(
            endpoint="site_wide", q='(subreddit:a or subreddit:b) AND "x"', rationale="b"
        )
        monkeypatch.setattr(
            station,
            "call_openai",
            _make_call_openai(JobPlan(reddit_queries=[*good, bad])),
        )
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        result = await run_query_expansion(spec)
        assert len(result.reddit_queries) == 25


class TestFallbackTable:
    """Every row of spec §10's failure table → QueryExpansionError."""

    async def test_call1_failure(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _call(**kwargs: Any) -> Any:
            raise RuntimeError("call #1 down")

        monkeypatch.setattr(station, "call_openai", _call)
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_all_phrase_searches_fail(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _search(phrases: list[str], **kwargs: Any) -> list[PhraseResult]:
            raise RuntimeError("reddit down")

        monkeypatch.setattr(station, "call_openai", _make_call_openai(_plan()))
        monkeypatch.setattr(station, "search_subreddits", _search)
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_zero_subs_survive_filtering(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _search(phrases: list[str], **kwargs: Any) -> list[PhraseResult]:
            return [
                PhraseResult(
                    phrase=p,
                    candidates=[SubredditCandidate(name="ghost", subreddit_type="private")],
                )
                for p in phrases
            ]

        monkeypatch.setattr(station, "call_openai", _make_call_openai(_plan()))
        monkeypatch.setattr(station, "search_subreddits", _search)
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_call2_failure(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _call(**kwargs: Any) -> Any:
            if kwargs["response_model"] is SubredditSearchPhrases:
                return SubredditSearchPhrases(phrases=["a", "b", "c"])
            raise RuntimeError("call #2 down")

        monkeypatch.setattr(station, "call_openai", _call)
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_too_few_valid_queries_after_tail(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = [_query(f"g{i}") for i in range(9)]
        bad = RedditQuerySpec(endpoint="site_wide", q="(subreddit:a or subreddit:b)", rationale="b")
        monkeypatch.setattr(
            station,
            "call_openai",
            _make_call_openai(JobPlan(reddit_queries=[*good, *[bad] * 17])),
        )
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)


class TestOffTableRejection:
    async def test_off_table_subs_are_stripped_from_selection(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _plan(subs=["startups", "ghost"])
        monkeypatch.setattr(station, "call_openai", _make_call_openai(plan))
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        result = await run_query_expansion(spec)
        assert "ghost" not in result.reddit_subreddits
        assert "startups" in result.reddit_subreddits


class TestTimeWindowOverride:
    async def test_forces_window_on_every_query(
        self, tmp_cache: Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        year_spec = JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year")
        mixed = [
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups) AND "q{i}"',
                rationale=f"r{i}",
                t=("month" if i % 2 else "week"),  # type: ignore[arg-type]
            )
            for i in range(25)
        ]
        monkeypatch.setattr(
            station, "call_openai", _make_call_openai(JobPlan(reddit_queries=mixed))
        )
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        result = await run_query_expansion(year_spec)
        assert {q.t for q in result.reddit_queries} == {"year"}


class TestBaselineSubredditMerge:
    async def test_baseline_appended_after_on_table_picks(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _plan(subs=["doggrooming", "groomers", "petbusiness"])
        monkeypatch.setattr(station, "call_openai", _make_call_openai(plan))
        monkeypatch.setattr(
            station,
            "search_subreddits",
            _make_search("doggrooming", "groomers", "petbusiness"),
        )
        result = await run_query_expansion(spec)
        for baseline in ("startups", "microsaas", "smallbusiness"):
            assert baseline in result.reddit_subreddits
        assert result.reddit_subreddits[:3] == [
            "doggrooming",
            "groomers",
            "petbusiness",
        ]

    async def test_no_duplicate_when_llm_picked_a_baseline(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _plan(subs=["smallbusiness", "Entrepreneur", "wholesale"])
        monkeypatch.setattr(station, "call_openai", _make_call_openai(plan))
        monkeypatch.setattr(
            station,
            "search_subreddits",
            _make_search("smallbusiness", "Entrepreneur", "wholesale"),
        )
        result = await run_query_expansion(spec)
        assert result.reddit_subreddits.count("smallbusiness") == 1
        assert "startups" in result.reddit_subreddits
        assert "microsaas" in result.reddit_subreddits
