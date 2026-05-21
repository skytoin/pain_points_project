from __future__ import annotations

from discovery.sources.hackernews import HackerNewsSource
from discovery.sources.reddit import RedditSource
from discovery.workers import build_default_registry


class TestBuildDefaultRegistry:
    def test_includes_reddit_adapter(self) -> None:
        registry = build_default_registry()
        assert "reddit" in registry
        assert isinstance(registry["reddit"], RedditSource)

    def test_includes_hackernews_adapter(self) -> None:
        registry = build_default_registry()
        assert "hackernews" in registry
        assert isinstance(registry["hackernews"], HackerNewsSource)

    def test_hackernews_adapter_constructed_without_credentials(self) -> None:
        """HN needs no creds; the registration line must construct
        HackerNewsSource() with no kwargs."""
        registry = build_default_registry()
        hn = registry["hackernews"]
        assert hasattr(hn, "aclose")
