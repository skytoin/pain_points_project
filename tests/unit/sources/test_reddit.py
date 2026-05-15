"""Tests for `discovery.sources.reddit`.

Two layers:

1. Pure helpers — name validation, URL building, junk filter,
   post→RawRecord converter. No HTTP, no async.
2. `RedditSource.fetch` — uses `httpx.MockTransport` (built-in, no extra
   deps) to simulate the .json endpoints. Sleep and rate limiting are
   injected as no-ops so tests run instantly.

See `.claude/skills/reddit-source/SKILL.md` for the operational rules
these tests are encoding.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from loguru import logger as _loguru

from discovery.sources.reddit import (
    RedditSource,
    build_query_url,
    keep_post,
    post_to_raw_record,
    validate_subreddit_name,
)

# --- Pure helpers --------------------------------------------------------


class TestValidateSubredditName:
    def test_strips_r_prefix(self) -> None:
        assert validate_subreddit_name("r/startups") == "startups"
        assert validate_subreddit_name("/r/startups") == "startups"

    def test_accepts_underscore_and_digits(self) -> None:
        assert validate_subreddit_name("micro_saas_2") == "micro_saas_2"

    def test_rejects_too_short(self) -> None:
        assert validate_subreddit_name("ab") is None

    def test_rejects_too_long(self) -> None:
        assert validate_subreddit_name("a" * 22) is None

    def test_rejects_spaces(self) -> None:
        assert validate_subreddit_name("Small Business") is None

    def test_rejects_special_characters(self) -> None:
        assert validate_subreddit_name("AI/ML") is None
        assert validate_subreddit_name("foo-bar") is None
        assert validate_subreddit_name("foo.bar") is None


class TestBuildQueryUrl:
    def test_per_sub_uses_subreddit_search_path(self) -> None:
        url = build_query_url(
            {
                "endpoint": "per_sub",
                "subreddit": "startups",
                "q": "wish there was",
                "sort": "top",
                "t": "month",
                "limit": 100,
            }
        )
        assert url.startswith("https://www.reddit.com/r/startups/search.json")

    def test_per_sub_sets_restrict_sr_true(self) -> None:
        url = build_query_url(
            {
                "endpoint": "per_sub",
                "subreddit": "startups",
                "q": "x",
                "sort": "top",
                "t": "month",
                "limit": 100,
            }
        )
        assert "restrict_sr=true" in url

    def test_site_wide_omits_restrict_sr(self) -> None:
        url = build_query_url(
            {
                "endpoint": "site_wide",
                "q": '(subreddit:startups OR subreddit:saas) AND "I would pay"',
                "sort": "top",
                "t": "month",
                "limit": 100,
            }
        )
        assert url.startswith("https://www.reddit.com/search.json")
        assert "restrict_sr" not in url

    def test_always_includes_raw_json_and_nsfw_filter(self) -> None:
        """`raw_json=1` keeps `&amp;` etc. unescaped; `include_over_18=false`
        is one of two NSFW filters (the other is `nsfw:no` in the query)."""
        url = build_query_url(
            {
                "endpoint": "per_sub",
                "subreddit": "startups",
                "q": "x",
                "sort": "top",
                "t": "month",
                "limit": 100,
            }
        )
        assert "raw_json=1" in url
        assert "include_over_18=false" in url


class TestKeepPost:
    def _base_post(self, **overrides: Any) -> dict[str, Any]:
        post = {
            "score": 50,
            "num_comments": 10,
            "over_18": False,
            "removed_by_category": None,
            "author": "alice",
            "title": "x",
            "selftext": "x",
            "permalink": "/r/startups/comments/abc/",
            "id": "abc",
            "subreddit": "startups",
        }
        post.update(overrides)
        return post

    def test_keeps_post_above_thresholds(self) -> None:
        assert keep_post(self._base_post(), min_score=5, min_comments=2)

    def test_drops_low_score(self) -> None:
        assert not keep_post(self._base_post(score=2), min_score=5, min_comments=2)

    def test_drops_low_comments(self) -> None:
        assert not keep_post(self._base_post(num_comments=1), min_score=5, min_comments=2)

    def test_drops_nsfw(self) -> None:
        assert not keep_post(self._base_post(over_18=True), min_score=5, min_comments=2)

    def test_drops_removed(self) -> None:
        assert not keep_post(
            self._base_post(removed_by_category="moderator"),
            min_score=5,
            min_comments=2,
        )

    def test_drops_deleted_author(self) -> None:
        assert not keep_post(self._base_post(author="[deleted]"), min_score=5, min_comments=2)


class TestPostToRawRecord:
    def test_uses_permalink_not_url_as_external_id(self) -> None:
        """Permalink dedupes reliably; the post `url` field points at the
        linked article (different threads can share the same `url`)."""
        post = {
            "id": "abc",
            "permalink": "/r/startups/comments/abc/the_post/",
            "url": "https://example.com/article",
            "score": 50,
            "num_comments": 10,
            "selftext": "x",
            "title": "y",
            "subreddit": "startups",
        }
        rec = post_to_raw_record(post)
        assert rec.external_id == "/r/startups/comments/abc/the_post/"
        assert rec.source == "reddit"

    def test_long_selftext_trimmed_to_200_chars_in_body(self) -> None:
        """Per skill item 15: trim selftext to ~200 chars before storing,
        to keep downstream LLM token cost bounded."""
        post = {
            "id": "abc",
            "permalink": "/r/startups/comments/abc/",
            "url": "https://x",
            "score": 50,
            "num_comments": 10,
            "selftext": "x" * 500,
            "title": "y",
            "subreddit": "startups",
        }
        rec = post_to_raw_record(post)
        assert len(rec.body["selftext"]) <= 201  # 200 + possible ellipsis


# --- Adapter integration ------------------------------------------------


def _mock_listing(post_ids: list[str], **post_overrides: Any) -> dict[str, Any]:
    """Build a minimal Reddit listing JSON with the given post IDs."""
    children = []
    for pid in post_ids:
        post = {
            "id": pid,
            "permalink": f"/r/startups/comments/{pid}/post_{pid}/",
            "url": f"https://reddit.com/r/startups/comments/{pid}/",
            "score": 50,
            "num_comments": 10,
            "over_18": False,
            "removed_by_category": None,
            "author": "alice",
            "title": f"Post {pid}",
            "selftext": "I would pay for this",
            "subreddit": "startups",
        }
        post.update(post_overrides)
        children.append({"kind": "t3", "data": post})
    return {"kind": "Listing", "data": {"children": children, "after": None}}


async def _noop_sleep(_: float) -> None:
    return None


def _client_from_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestRedditSourceFetch:
    async def test_happy_path_returns_records(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_listing(["a1", "a2"]))

        source = RedditSource(
            user_agent="discovery-tests/0.1",
            client=_client_from_handler(handler),
            sleep=_noop_sleep,
        )
        records = await source.fetch(
            {
                "queries": [
                    {
                        "endpoint": "per_sub",
                        "subreddit": "startups",
                        "q": "wish there was",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    }
                ]
            }
        )
        assert len(records) == 2
        assert all(r.source == "reddit" for r in records)
        assert all(r.external_id.startswith("/r/startups/comments/") for r in records)

    async def test_sends_descriptive_user_agent(self) -> None:
        """Per skill item 2: User-Agent must not be generic, or Reddit
        silently throttles. Adapter must forward whatever user_agent the
        caller passed."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, json=_mock_listing([]))

        source = RedditSource(
            user_agent="my-app/1.0 (contact: me@example.com)",
            client=_client_from_handler(handler),
            sleep=_noop_sleep,
        )
        await source.fetch(
            {
                "queries": [
                    {
                        "endpoint": "per_sub",
                        "subreddit": "startups",
                        "q": "x",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    }
                ]
            }
        )
        assert seen["ua"] == "my-app/1.0 (contact: me@example.com)"

    async def test_partial_success_returns_what_worked(self) -> None:
        """Per skill item 17: one query failing must not throw away other
        queries' results."""

        def handler(request: httpx.Request) -> httpx.Response:
            # The `saas` subreddit always 500s — every other URL succeeds.
            if "/r/saas/" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(200, json=_mock_listing(["good"]))

        source = RedditSource(
            user_agent="discovery-tests/0.1",
            client=_client_from_handler(handler),
            sleep=_noop_sleep,
            max_retries=0,
        )
        records = await source.fetch(
            {
                "queries": [
                    {
                        "endpoint": "per_sub",
                        "subreddit": "startups",
                        "q": "a",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    },
                    {
                        "endpoint": "per_sub",
                        "subreddit": "saas",
                        "q": "b",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    },
                    {
                        "endpoint": "per_sub",
                        "subreddit": "microsaas",
                        "q": "c",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    },
                ]
            }
        )
        assert len(records) == 2  # 2 of 3 queries succeeded

    async def test_drops_invalid_subreddit_silently(self) -> None:
        """Per skill item 10: invalid subreddit names get filtered silently
        at planning time; the adapter never hits Reddit with a bad name."""
        hit_count = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            hit_count["n"] += 1
            return httpx.Response(200, json=_mock_listing([]))

        source = RedditSource(
            user_agent="discovery-tests/0.1",
            client=_client_from_handler(handler),
            sleep=_noop_sleep,
        )
        await source.fetch(
            {
                "queries": [
                    {
                        "endpoint": "per_sub",
                        "subreddit": "Small Business",  # invalid (space)
                        "q": "x",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    },
                    {
                        "endpoint": "per_sub",
                        "subreddit": "startups",
                        "q": "x",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    },
                ]
            }
        )
        # The invalid query was dropped; only one HTTP call happened.
        assert hit_count["n"] == 1

    async def test_filters_junk_posts_below_quality_floor(self) -> None:
        """Per skill item 13: drop low-score / low-comment / NSFW / removed
        posts before they reach the LLM downstream."""

        def handler(_: httpx.Request) -> httpx.Response:
            payload = {
                "kind": "Listing",
                "data": {
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "id": "good",
                                "permalink": "/r/startups/comments/good/",
                                "url": "https://x",
                                "score": 50,
                                "num_comments": 10,
                                "over_18": False,
                                "removed_by_category": None,
                                "author": "alice",
                                "title": "good post",
                                "selftext": "I would pay",
                                "subreddit": "startups",
                            },
                        },
                        {
                            "kind": "t3",
                            "data": {
                                "id": "bad",
                                "permalink": "/r/startups/comments/bad/",
                                "url": "https://x",
                                "score": 1,  # below floor
                                "num_comments": 0,
                                "over_18": False,
                                "removed_by_category": None,
                                "author": "bob",
                                "title": "junk",
                                "selftext": "",
                                "subreddit": "startups",
                            },
                        },
                    ],
                    "after": None,
                },
            }
            return httpx.Response(200, json=payload)

        source = RedditSource(
            user_agent="discovery-tests/0.1",
            client=_client_from_handler(handler),
            sleep=_noop_sleep,
        )
        records = await source.fetch(
            {
                "queries": [
                    {
                        "endpoint": "per_sub",
                        "subreddit": "startups",
                        "q": "x",
                        "sort": "top",
                        "t": "month",
                        "limit": 100,
                    }
                ]
            }
        )
        assert len(records) == 1
        assert records[0].external_id == "/r/startups/comments/good/"


class TestRedditSourceLogging:
    """Skill item 21: per-query log line carrying URL, status, response
    time, count before AND after the engagement filter. This is the
    diagnostic foundation for debugging low-yield runs.
    """

    async def test_run_one_logs_structured_query_summary(self) -> None:
        captured: list[dict[str, Any]] = []

        def sink(message: Any) -> None:
            captured.append(dict(message.record["extra"]))

        sink_id = _loguru.add(sink, level="DEBUG")
        try:

            def handler(_: httpx.Request) -> httpx.Response:
                # 3 children, 2 of which pass the engagement floor
                return httpx.Response(
                    200,
                    json={
                        "kind": "Listing",
                        "data": {
                            "children": [
                                {
                                    "kind": "t3",
                                    "data": {
                                        "id": "g1",
                                        "permalink": "/r/x/comments/g1/",
                                        "url": "https://x",
                                        "score": 50,
                                        "num_comments": 10,
                                        "over_18": False,
                                        "removed_by_category": None,
                                        "author": "a",
                                        "title": "kept 1",
                                        "selftext": "",
                                        "subreddit": "startups",
                                    },
                                },
                                {
                                    "kind": "t3",
                                    "data": {
                                        "id": "g2",
                                        "permalink": "/r/x/comments/g2/",
                                        "url": "https://x",
                                        "score": 50,
                                        "num_comments": 10,
                                        "over_18": False,
                                        "removed_by_category": None,
                                        "author": "a",
                                        "title": "kept 2",
                                        "selftext": "",
                                        "subreddit": "startups",
                                    },
                                },
                                {
                                    "kind": "t3",
                                    "data": {
                                        "id": "lo",
                                        "permalink": "/r/x/comments/lo/",
                                        "url": "https://x",
                                        "score": 1,  # below threshold
                                        "num_comments": 0,
                                        "over_18": False,
                                        "removed_by_category": None,
                                        "author": "a",
                                        "title": "dropped",
                                        "selftext": "",
                                        "subreddit": "startups",
                                    },
                                },
                            ],
                            "after": None,
                        },
                    },
                )

            source = RedditSource(
                user_agent="discovery-tests/0.1",
                client=_client_from_handler(handler),
                sleep=_noop_sleep,
            )
            await source.fetch(
                {
                    "queries": [
                        {
                            "endpoint": "per_sub",
                            "subreddit": "startups",
                            "q": "wish there was",
                            "sort": "top",
                            "t": "year",
                            "limit": 100,
                        }
                    ]
                }
            )

            # Find the per-query log line — identified by the URL field
            query_logs = [c for c in captured if "url" in c and "count_after_filter" in c]
            assert query_logs, f"no per-query log line found; captured: {captured}"
            log = query_logs[0]
            assert log["status"] == 200
            assert log["count_before_filter"] == 3
            assert log["count_after_filter"] == 2
            assert log["endpoint"] == "per_sub"
            assert log["subreddit"] == "startups"
            assert log["elapsed_ms"] >= 0
            assert "/r/startups/search.json" in log["url"]
        finally:
            _loguru.remove(sink_id)
