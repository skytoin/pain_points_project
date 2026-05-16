"""Tests for the process-wide shared Reddit limiter (spec §11 item 3)."""

from __future__ import annotations

import httpx
from aiolimiter import AsyncLimiter

from discovery.sources.reddit import RedditSource
from discovery.sources.reddit_ratelimit import (
    get_reddit_limiter,
    reset_reddit_limiter,
)


class TestSingleton:
    def test_get_returns_same_instance(self) -> None:
        assert get_reddit_limiter() is get_reddit_limiter()

    def test_reset_makes_a_fresh_instance(self) -> None:
        first = get_reddit_limiter()
        reset_reddit_limiter()
        assert get_reddit_limiter() is not first


class TestRedditSourceWiring:
    async def test_defaults_to_the_shared_singleton(self) -> None:
        source = RedditSource(user_agent="discovery-tests/0.1")
        try:
            assert source._limiter is get_reddit_limiter()
        finally:
            await source.aclose()

    async def test_injected_limiter_overrides_the_singleton(self) -> None:
        injected = AsyncLimiter(99, 1)
        source = RedditSource(
            user_agent="discovery-tests/0.1",
            client=httpx.AsyncClient(),
            limiter=injected,
        )
        try:
            assert source._limiter is injected
            assert source._limiter is not get_reddit_limiter()
        finally:
            await source.aclose()
