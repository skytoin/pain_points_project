from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.base import RawRecord
from discovery.sources.hackernews import (
    HackerNewsSource,
    build_search_url,
    hit_to_raw_record,
    keep_hit,
)


def _query(
    endpoint: str = "search",
    query: str = "Personal CRM",
    tags: str = "story",
    numeric_filters: str = "created_at_i>1700000000,points>5,num_comments>3",
    hits_per_page: int = 30,
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "query": query,
        "tags": tags,
        "numeric_filters": numeric_filters,
        "hits_per_page": hits_per_page,
    }


class TestBuildSearchUrl:
    def test_routes_search_endpoint(self) -> None:
        url = build_search_url(_query(endpoint="search"))
        assert url.startswith("https://hn.algolia.com/api/v1/search?")

    def test_routes_search_by_date_endpoint(self) -> None:
        url = build_search_url(
            _query(endpoint="search_by_date", tags="show_hn", numeric_filters="created_at_i>1")
        )
        assert url.startswith("https://hn.algolia.com/api/v1/search_by_date?")

    def test_serializes_tags(self) -> None:
        url = build_search_url(_query(tags="story"))
        assert "tags=story" in url

    def test_serializes_numeric_filters_as_camelcase(self) -> None:
        # snake_case key in the compiled dict; Algolia expects camelCase in the URL.
        url = build_search_url(_query(numeric_filters="points>5,num_comments>3"))
        assert "numericFilters=" in url
        assert "numeric_filters" not in url

    def test_hits_per_page_passes_through(self) -> None:
        url = build_search_url(_query(hits_per_page=30))
        assert "hitsPerPage=30" in url

    def test_page_pinned_to_zero(self) -> None:
        url = build_search_url(_query())
        # Spec §11: always pin page=0 in code, do not rely on Algolia's
        # default. Top 30 by relevance/date is what we want, no paging.
        assert "page=0" in url

    def test_query_url_encoded(self) -> None:
        url = build_search_url(_query(query="Personal CRM"))
        # urlencode encodes space as `+`.
        assert "query=Personal+CRM" in url

    def test_numeric_filters_url_encoded(self) -> None:
        # `>` becomes %3E in URL encoding.
        url = build_search_url(_query(numeric_filters="created_at_i>1700000000"))
        assert "created_at_i%3E1700000000" in url

    def test_unknown_endpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown HN endpoint"):
            build_search_url(_query(endpoint="search_by_relevance"))


class TestKeepHit:
    def test_keeps_normal_hit(self) -> None:
        assert keep_hit({"objectID": "12345", "title": "x"})

    def test_drops_hit_without_object_id(self) -> None:
        """Defensive -- Algolia always returns objectID, but if a hit
        ever lacked it we couldn't dedupe and Bronze would break."""
        assert not keep_hit({"title": "x"})


class TestHitToRawRecord:
    def test_external_id_is_object_id_string(self) -> None:
        hit = {
            "objectID": "12345",
            "title": "x",
            "url": "https://example.com",
            "points": 100,
            "num_comments": 20,
        }
        rec = hit_to_raw_record(hit)
        assert rec.external_id == "12345"
        assert rec.source == "hackernews"

    def test_body_is_verbatim_no_trimming(self) -> None:
        """Locked decision (spec §3): Bronze stores raw, Wave 2 parses.
        Adapter MUST NOT modify, trim, or normalize the hit."""
        long_title = "x" * 500
        hit = {
            "objectID": "1",
            "title": long_title,
            "_tags": ["story", "ask_hn"],
            "story_text": "y" * 1000,
        }
        rec = hit_to_raw_record(hit)
        assert rec.body == hit
        assert rec.body["title"] == long_title  # no trimming
        assert rec.body["story_text"] == "y" * 1000

    def test_ask_hn_post_with_null_url_still_yields_valid_external_id(self) -> None:
        """Ask HN / Show HN text posts often carry a null `url`. We
        rely on objectID for external_id, so dedupe still works.
        Wave 2 handles the permalink fallback."""
        hit = {"objectID": "9876", "title": "Ask HN: ...", "url": None}
        rec = hit_to_raw_record(hit)
        assert rec.external_id == "9876"
        assert rec.body["url"] is None  # verbatim -- no fallback in the adapter

    def test_returns_real_raw_record_instance(self) -> None:
        rec = hit_to_raw_record({"objectID": "7", "title": "x"})
        assert isinstance(rec, RawRecord)


def _client_from_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fast_limiter() -> AsyncLimiter:
    """Effectively-unbounded limiter for tests -- production uses
    AsyncLimiter(5, 1) for politeness."""
    return AsyncLimiter(max_rate=1000, time_period=1)


def _mock_hn_response(hit_ids: list[str]) -> dict[str, Any]:
    """Build a minimal Algolia HN response."""
    return {
        "hits": [
            {
                "objectID": hid,
                "title": f"Title {hid}",
                "url": f"https://example.com/{hid}",
                "points": 100,
                "num_comments": 20,
                "author": "alice",
                "created_at": "2026-05-01T00:00:00Z",
                "_tags": ["story"],
            }
            for hid in hit_ids
        ],
        "nbHits": len(hit_ids),
    }


class TestHackerNewsSourceFetch:
    async def test_happy_path_returns_records(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_hn_response(["a1", "a2"]))

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        records = await source.fetch({"queries": [_query()]})

        assert len(records) == 2
        assert all(r.source == "hackernews" for r in records)
        assert {r.external_id for r in records} == {"a1", "a2"}

    async def test_partial_success_returns_what_worked(self) -> None:
        """Locked partial-success contract (spec §11). One failed query
        does not poison the others."""
        counter = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "query=fails" in str(request.url):
                return httpx.Response(500)
            counter["n"] += 1
            return httpx.Response(200, json=_mock_hn_response([f"g{counter['n']}"]))

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        records = await source.fetch(
            {
                "queries": [
                    _query(query="ok"),
                    _query(query="fails"),
                    _query(query="ok2"),
                ],
            }
        )

        assert len(records) == 2  # 2 of 3 queries succeeded

    async def test_all_queries_fail_raises_first_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch({"queries": [_query(), _query(query="another")]})

    async def test_no_retry_on_5xx(self) -> None:
        """Locked decision (spec §11 / §16): HN does NOT retry. A
        single 500 records ONE error; the adapter does not re-hit the
        URL -- this divergence from the source-adapter umbrella is
        deliberate."""
        hit_count = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            hit_count["n"] += 1
            return httpx.Response(500)

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch({"queries": [_query()]})

        assert hit_count["n"] == 1  # exactly one HTTP call

    async def test_filters_hits_without_object_id(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {"objectID": "g1", "title": "good"},
                        {"title": "no objectID -- dropped"},
                        {"objectID": "g2", "title": "good"},
                    ],
                    "nbHits": 3,
                },
            )

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        records = await source.fetch({"queries": [_query()]})

        assert len(records) == 2
        assert {r.external_id for r in records} == {"g1", "g2"}


class TestHackerNewsSourceAclose:
    async def test_aclose_closes_owned_client(self) -> None:
        source = HackerNewsSource(limiter=_fast_limiter())  # owns its own client
        assert not source._client.is_closed
        await source.aclose()
        assert source._client.is_closed

    async def test_aclose_does_not_close_injected_client(self) -> None:
        injected = httpx.AsyncClient()
        try:
            source = HackerNewsSource(client=injected, limiter=_fast_limiter())
            await source.aclose()
            assert not injected.is_closed
        finally:
            # Test owns the injected client; close it ourselves so
            # `filterwarnings=error` doesn't trip on GC.
            await injected.aclose()


class TestHackerNewsSourceLogging:
    async def test_per_query_log_line_carries_diagnostic_fields(self) -> None:
        """Spec §11 / skill item 21 analog: per-query log line carries
        url, status, response time, count before AND after filter,
        endpoint, tags."""
        captured: list[dict[str, Any]] = []

        def sink(message: Any) -> None:
            captured.append(dict(message.record["extra"]))

        sink_id = logger.add(sink, level="DEBUG")
        try:

            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_mock_hn_response(["g1", "g2"]))

            source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
            await source.fetch({"queries": [_query(endpoint="search_by_date", tags="show_hn")]})

            query_logs = [c for c in captured if "url" in c and "count_after_filter" in c]
            assert query_logs, f"no per-query log line found; captured: {captured}"
            log = query_logs[0]
            assert log["status"] == 200
            assert log["count_before_filter"] == 2
            assert log["count_after_filter"] == 2
            assert log["endpoint"] == "search_by_date"
            assert log["tags"] == "show_hn"
            assert log["elapsed_ms"] >= 0
            assert "hn.algolia.com/api/v1/search_by_date" in log["url"]
        finally:
            logger.remove(sink_id)
