from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.youtube import (
    CommentsDisabled,
    YouTubeQuotaExceeded,
    YouTubeRateLimited,
    YouTubeSource,
    _comment_likes,
    _comment_text,
    build_comments_url,
    build_search_url,
    build_videos_url,
    comment_to_raw_record,
    extract_video_ids,
    keep_comment,
    search_hit_to_raw_record,
    video_to_raw_record,
    viewcount_of,
)

_KEY = "test-key"


def _client_from_handler(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fast_limiter() -> AsyncLimiter:
    return AsyncLimiter(max_rate=1000, time_period=1)


async def _noop_sleep(_: float) -> None:
    return None


def _search_query(
    query: str = "why I quit cleaning",
    published_after: str | None = "2026-04-22T00:00:00Z",
) -> dict[str, Any]:
    return {
        "query": query,
        "order": "relevance",
        "type": "video",
        "part": "snippet",
        "published_after": published_after,
        "max_results": 50,
    }


class TestBuildUrls:
    def test_search_url_base_and_key(self) -> None:
        url = build_search_url(_search_query(), _KEY)
        assert url.startswith("https://www.googleapis.com/youtube/v3/search?")
        assert "key=test-key" in url

    def test_search_url_carries_params(self) -> None:
        url = build_search_url(_search_query(query="day in the life plumber"), _KEY)
        assert "q=day+in+the+life+plumber" in url
        assert "type=video" in url
        assert "order=relevance" in url
        assert "part=snippet" in url
        assert "maxResults=50" in url

    def test_search_url_includes_published_after_when_set(self) -> None:
        url = build_search_url(_search_query(published_after="2026-04-22T00:00:00Z"), _KEY)
        assert "publishedAfter=2026-04-22T00%3A00%3A00Z" in url

    def test_search_url_omits_published_after_when_none(self) -> None:
        url = build_search_url(_search_query(published_after=None), _KEY)
        assert "publishedAfter" not in url

    def test_videos_url_csv_ids_and_parts(self) -> None:
        url = build_videos_url(["vid1", "vid2", "vid3"], _KEY)
        assert url.startswith("https://www.googleapis.com/youtube/v3/videos?")
        assert "part=snippet%2Cstatistics" in url  # 'snippet,statistics' url-encoded
        assert "id=vid1%2Cvid2%2Cvid3" in url
        assert "key=test-key" in url

    def test_comments_url(self) -> None:
        url = build_comments_url("vid1", _KEY)
        assert url.startswith("https://www.googleapis.com/youtube/v3/commentThreads?")
        assert "videoId=vid1" in url
        assert "part=snippet" in url
        assert "order=relevance" in url
        assert "maxResults=100" in url
        assert "key=test-key" in url


class TestRecordHelpers:
    def test_extract_video_ids_skips_non_video_items(self) -> None:
        payload = {
            "items": [
                {"id": {"kind": "youtube#video", "videoId": "v1"}},
                {"id": {"kind": "youtube#channel", "channelId": "c1"}},  # no videoId
                {"id": {"kind": "youtube#video", "videoId": "v2"}},
            ]
        }
        assert extract_video_ids(payload) == ["v1", "v2"]

    def test_video_to_raw_record_verbatim(self) -> None:
        video = {
            "kind": "youtube#video",
            "id": "v1",
            "snippet": {"title": "t"},
            "statistics": {"viewCount": "1000"},
        }
        rec = video_to_raw_record(video)
        assert rec.source == "youtube"
        assert rec.external_id == "v1"
        assert rec.body == video  # verbatim

    def test_comment_to_raw_record_verbatim_carries_video_id(self) -> None:
        thread = {
            "kind": "youtube#commentThread",
            "id": "ct1",
            "snippet": {"videoId": "v1", "topLevelComment": {"snippet": {"textDisplay": "x"}}},
        }
        rec = comment_to_raw_record(thread)
        assert rec.source == "youtube"
        assert rec.external_id == "ct1"
        assert rec.body["snippet"]["videoId"] == "v1"

    def test_search_hit_to_raw_record_uses_video_id(self) -> None:
        item = {"id": {"videoId": "v1"}, "snippet": {"title": "t"}}
        rec = search_hit_to_raw_record(item)
        assert rec.source == "youtube"
        assert rec.external_id == "v1"
        assert rec.body == item

    def test_viewcount_of_parses_string(self) -> None:
        assert viewcount_of({"statistics": {"viewCount": "1234"}}) == 1234

    def test_viewcount_of_missing_defaults_zero(self) -> None:
        assert viewcount_of({"statistics": {}}) == 0
        assert viewcount_of({}) == 0


def _error_response(status: int, reason: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"errors": [{"reason": reason}], "code": status}})


def _src(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> YouTubeSource:
    return YouTubeSource(
        api_key=_KEY,
        client=_client_from_handler(handler),
        limiter=_fast_limiter(),
        sleep=_noop_sleep,
        **kw,
    )


class TestGetJson:
    async def test_returns_parsed_json_on_200(self) -> None:
        src = _src(lambda _: httpx.Response(200, json={"ok": True}))
        try:
            assert await src._get_json("https://x/", kind="search") == {"ok": True}
        finally:
            await src.aclose()

    async def test_quota_exceeded_raises_first_call_no_retry(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return _error_response(403, "quotaExceeded")

        src = _src(handler)
        try:
            with pytest.raises(YouTubeQuotaExceeded):
                await src._get_json("https://x/", kind="search")
            assert calls["n"] == 1
        finally:
            await src.aclose()

    async def test_comments_disabled_raises_first_call(self) -> None:
        src = _src(lambda _: _error_response(403, "commentsDisabled"))
        try:
            with pytest.raises(CommentsDisabled):
                await src._get_json("https://x/", kind="search")
        finally:
            await src.aclose()

    async def test_transient_500_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})

        src = _src(handler)
        try:
            assert await src._get_json("https://x/", kind="search") == {"ok": True}
            assert calls["n"] == 2  # retried once
        finally:
            await src.aclose()

    async def test_rate_limit_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return _error_response(403, "rateLimitExceeded")
            return httpx.Response(200, json={"ok": True})

        src = _src(handler)
        try:
            assert await src._get_json("https://x/", kind="search") == {"ok": True}
            assert calls["n"] == 2
        finally:
            await src.aclose()

    async def test_http_429_is_retried_then_succeeds(self) -> None:
        """A real HTTP 429 (rate-limit) is retryable with backoff, not an
        immediate failure (spec section 4/10)."""
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return _error_response(429, "userRateLimitExceeded")
            return httpx.Response(200, json={"ok": True})

        src = _src(handler)
        try:
            assert await src._get_json("https://x/", kind="search") == {"ok": True}
            assert calls["n"] == 2  # retried once
        finally:
            await src.aclose()

    async def test_persistent_rate_limit_raises_youtube_rate_limited(self) -> None:
        """rateLimitExceeded on every attempt exhausts the budget and
        raises YouTubeRateLimited (a transient stop the helpers catch)."""
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return _error_response(403, "rateLimitExceeded")

        src = _src(handler, max_retries=2)
        try:
            with pytest.raises(YouTubeRateLimited):
                await src._get_json("https://x/", kind="search")
            assert calls["n"] == 3  # 1 + 2 retries
        finally:
            await src.aclose()

    async def test_persistent_5xx_raises_after_budget(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503)

        src = _src(handler, max_retries=2)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await src._get_json("https://x/", kind="search")
            assert calls["n"] == 3  # 1 + 2 retries
        finally:
            await src.aclose()


class TestAclose:
    async def test_aclose_closes_owned_client(self) -> None:
        src = YouTubeSource(api_key=_KEY, limiter=_fast_limiter())
        assert not src._client.is_closed
        await src.aclose()
        assert src._client.is_closed

    async def test_aclose_does_not_close_injected_client(self) -> None:
        injected = httpx.AsyncClient()
        try:
            src = YouTubeSource(api_key=_KEY, client=injected, limiter=_fast_limiter())
            await src.aclose()
            assert not injected.is_closed
        finally:
            await injected.aclose()


def _single_param(url: str, name: str) -> str:
    return parse_qs(urlparse(url).query).get(name, [""])[0]


def _ids_from_query(url: str, name: str) -> list[str]:
    raw = _single_param(url, name)
    return raw.split(",") if raw else []


def _routing_handler(
    *,
    search_pages: dict[str, list[str]],
    stats: dict[str, str] | None = None,
    disabled_videos: set[str] | None = None,
    quota_on: set[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """search_pages: q-substring -> list of videoIds. stats: vid -> viewCount.
    quota_on: substrings that should 403 quotaExceeded. disabled_videos:
    videoIds whose comment call 403s commentsDisabled."""
    stats = stats or {}
    disabled_videos = disabled_videos or set()
    quota_on = quota_on or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/search?" in url:
            q = _single_param(url, "q")  # decoded; robust vs `+`-encoding
            if any(tok in q for tok in quota_on):
                return _error_response(403, "quotaExceeded")
            for needle, ids in search_pages.items():
                if needle in q:
                    return httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "kind": "youtube#searchResult",
                                    "id": {"kind": "youtube#video", "videoId": v},
                                    "snippet": {"title": v},
                                }
                                for v in ids
                            ]
                        },
                    )
            return httpx.Response(200, json={"items": []})
        if "/videos?" in url:
            ids = _ids_from_query(url, "id")
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "youtube#video",
                            "id": v,
                            "snippet": {"title": v},
                            "statistics": {"viewCount": stats.get(v, "0")},
                        }
                        for v in ids
                    ]
                },
            )
        if "/commentThreads?" in url:
            vid = _single_param(url, "videoId")
            if vid in disabled_videos:
                return _error_response(403, "commentsDisabled")
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "youtube#commentThread",
                            "id": f"{vid}-c1",
                            "snippet": {
                                "videoId": vid,
                                "topLevelComment": {
                                    "snippet": {
                                        "textOriginal": (
                                            "a substantive comment about a real "
                                            "problem with the device"
                                        ),
                                        "likeCount": 0,
                                    }
                                },
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return handler


class TestFetch:
    async def test_no_key_is_noop(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"items": []})

        src = YouTubeSource(
            api_key=None,
            client=_client_from_handler(handler),
            limiter=_fast_limiter(),
            sleep=_noop_sleep,
        )
        try:
            assert await src.fetch({"queries": [_search_query()]}) == []
            assert calls["n"] == 0
        finally:
            await src.aclose()

    async def test_happy_path_returns_video_and_comment_records(self) -> None:
        handler = _routing_handler(
            search_pages={"why": ["v1", "v2"]}, stats={"v1": "500", "v2": "10"}
        )
        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query(query="why I quit cleaning")]})
            kinds = {r.body.get("kind") for r in records}
            assert "youtube#video" in kinds
            assert "youtube#commentThread" in kinds
            video_ids = {r.external_id for r in records if r.body.get("kind") == "youtube#video"}
            assert video_ids == {"v1", "v2"}
            assert all(r.source == "youtube" for r in records)
        finally:
            await src.aclose()

    async def test_quota_stop_on_second_search_skips_third(self) -> None:
        """query 2 hits the wall -> query 3 is NOT attempted; query-1's
        video still flows through enrichment + comments."""
        searched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                q = _single_param(url, "q")
                searched.append(q)
                if "wall" in q:
                    return _error_response(403, "quotaExceeded")
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": {"kind": "youtube#video", "videoId": "v1"}, "snippet": {}}]
                    },
                )
            if "/videos?" in url:
                ids = _ids_from_query(url, "id")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#video",
                                "id": v,
                                "snippet": {},
                                "statistics": {"viewCount": "1"},
                            }
                            for v in ids
                        ]
                    },
                )
            if "/commentThreads?" in url:
                vid = _single_param(url, "videoId")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#commentThread",
                                "id": f"{vid}-c",
                                "snippet": {"videoId": vid},
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        src = _src(handler)
        try:
            records = await src.fetch(
                {
                    "queries": [
                        _search_query(query="first ok"),
                        _search_query(query="wall hit"),
                        _search_query(query="third never"),
                    ]
                }
            )
            assert searched == ["first ok", "wall hit"]  # query 3 never fired
            assert any(r.body.get("kind") == "youtube#video" for r in records)
        finally:
            await src.aclose()

    async def test_enrichment_quota_stop_falls_back_to_search_hits(self) -> None:
        """videos.list 403 quotaExceeded -> un-enriched ids stored as
        kind=youtube#searchResult; NO comment records (no stats to rank,
        quota gone)."""
        commented = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#searchResult",
                                "id": {"kind": "youtube#video", "videoId": "v1"},
                                "snippet": {"title": "t"},
                            }
                        ]
                    },
                )
            if "/videos?" in url:
                return _error_response(403, "quotaExceeded")
            if "/commentThreads?" in url:
                commented["n"] += 1
                return httpx.Response(200, json={"items": []})
            return httpx.Response(404)

        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query()]})
            assert {r.body.get("kind") for r in records} == {"youtube#searchResult"}
            assert records[0].external_id == "v1"
            assert commented["n"] == 0  # comment harvest skipped after enrichment quota-stop
        finally:
            await src.aclose()

    async def test_comments_disabled_skips_one_video(self) -> None:
        """commentsDisabled on v1 -> v1 skipped, v2 harvested; BOTH videos
        still stored."""
        handler = _routing_handler(
            search_pages={"why": ["v1", "v2"]},
            stats={"v1": "100", "v2": "50"},
            disabled_videos={"v1"},
        )
        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query(query="why")]})
            comment_vids = {
                r.body["snippet"]["videoId"]
                for r in records
                if r.body.get("kind") == "youtube#commentThread"
            }
            video_ids = {r.external_id for r in records if r.body.get("kind") == "youtube#video"}
            assert comment_vids == {"v2"}
            assert video_ids == {"v1", "v2"}
        finally:
            await src.aclose()

    async def test_top_k_limits_comment_videos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the COMMENT_TOP_K highest-view videos get a comment call."""
        import discovery.sources.youtube as yt  # noqa: PLC0415

        monkeypatch.setattr(yt, "COMMENT_TOP_K", 1)
        commented: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": {"kind": "youtube#video", "videoId": v}, "snippet": {}}
                            for v in ["low", "high"]
                        ]
                    },
                )
            if "/videos?" in url:
                ids = _ids_from_query(url, "id")
                views = {"low": "5", "high": "9999"}
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#video",
                                "id": v,
                                "snippet": {},
                                "statistics": {"viewCount": views[v]},
                            }
                            for v in ids
                        ]
                    },
                )
            if "/commentThreads?" in url:
                commented.append(_single_param(url, "videoId"))
                return httpx.Response(200, json={"items": []})
            return httpx.Response(404)

        src = _src(handler)
        try:
            await src.fetch({"queries": [_search_query()]})
            assert commented == ["high"]  # top-1 by viewcount only
        finally:
            await src.aclose()

    async def test_all_searches_fail_raises(self) -> None:
        src = _src(lambda _: httpx.Response(500), max_retries=0)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await src.fetch({"queries": [_search_query(), _search_query(query="other")]})
        finally:
            await src.aclose()

    async def test_persistent_rate_limit_on_one_query_is_partial_success(self) -> None:
        """A query rate-limited on EVERY attempt degrades to a failed query
        (partial success), not a crash: the other query's records survive."""

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                q = _single_param(url, "q")
                if "throttled" in q:
                    return _error_response(403, "rateLimitExceeded")  # every attempt
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": {"kind": "youtube#video", "videoId": "v1"}, "snippet": {}}]
                    },
                )
            if "/videos?" in url:
                ids = _ids_from_query(url, "id")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#video",
                                "id": v,
                                "snippet": {},
                                "statistics": {"viewCount": "1"},
                            }
                            for v in ids
                        ]
                    },
                )
            if "/commentThreads?" in url:
                vid = _single_param(url, "videoId")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#commentThread",
                                "id": f"{vid}-c",
                                "snippet": {"videoId": vid},
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        src = _src(handler, max_retries=2)
        try:
            records = await src.fetch(
                {
                    "queries": [
                        _search_query(query="throttled query"),
                        _search_query(query="ok query"),
                    ]
                }
            )
            video_ids = {r.external_id for r in records if r.body.get("kind") == "youtube#video"}
            assert video_ids == {"v1"}  # ok query's record survived; no crash
        finally:
            await src.aclose()

    async def test_enrichment_quota_stop_spans_two_batches(self) -> None:
        """>50 ids -> enrichment spans 2 batches. Batch 1 enriches; batch 2
        quota-stops -> youtube#video rows for batch-1 ids, youtube#searchResult
        fallback ONLY for batch-2 ids, and no comment calls for the latter."""
        batch1 = [f"a{i}" for i in range(50)]
        batch2 = [f"b{i}" for i in range(10)]
        all_ids = batch1 + batch2
        commented: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#searchResult",
                                "id": {"kind": "youtube#video", "videoId": v},
                                "snippet": {"title": v},
                            }
                            for v in all_ids
                        ]
                    },
                )
            if "/videos?" in url:
                ids = _ids_from_query(url, "id")
                if any(i.startswith("b") for i in ids):  # the second batch
                    return _error_response(403, "quotaExceeded")
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "kind": "youtube#video",
                                "id": v,
                                "snippet": {},
                                "statistics": {"viewCount": "1"},
                            }
                            for v in ids
                        ]
                    },
                )
            if "/commentThreads?" in url:
                commented.append(_single_param(url, "videoId"))
                return httpx.Response(200, json={"items": []})
            return httpx.Response(404)

        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query()]})
            video_ids = {r.external_id for r in records if r.body.get("kind") == "youtube#video"}
            fallback_ids = {
                r.external_id for r in records if r.body.get("kind") == "youtube#searchResult"
            }
            assert video_ids == set(batch1)  # batch 1 enriched verbatim
            assert fallback_ids == set(batch2)  # batch 2 stored as search hits
            assert all(c.startswith("a") for c in commented)  # no comments for un-enriched ids
        finally:
            await src.aclose()


def _thread(
    text: str | None = "x",
    likes: int | str | None = 0,
    tid: str = "ct1",
    *,
    use_display: bool = False,
) -> dict[str, Any]:
    """Build a youtube#commentThread with a top-level comment for filter tests."""
    snip: dict[str, Any] = {}
    if text is not None:
        snip["textDisplay" if use_display else "textOriginal"] = text
    if likes is not None:
        snip["likeCount"] = likes
    return {
        "kind": "youtube#commentThread",
        "id": tid,
        "snippet": {"videoId": "v1", "topLevelComment": {"snippet": snip}},
    }


class TestKeepComment:
    def test_drops_emoji_only(self) -> None:
        assert keep_comment(_thread("\U0001f605\U0001f605", 0)) is False

    def test_drops_symbol_and_number_only(self) -> None:
        assert keep_comment(_thread("!!! 123 ???", 0)) is False

    def test_drops_short_low_engagement(self) -> None:
        assert keep_comment(_thread("nice video", 0)) is False  # 10 chars, 0 likes

    def test_keeps_short_but_upvoted(self) -> None:
        # 22 chars (< 45) but >= 7 likes -> rescued
        assert keep_comment(_thread("this bricked my router", 7)) is True

    def test_keeps_long_zero_likes(self) -> None:
        assert keep_comment(_thread("a" * 45, 0)) is True

    def test_boundary_44_dropped_45_kept(self) -> None:
        assert keep_comment(_thread("a" * 44, 0)) is False
        assert keep_comment(_thread("a" * 45, 0)) is True

    def test_keeps_non_english_text(self) -> None:
        # Cyrillic, >= 45 chars, isalpha() True for letters of any script
        assert keep_comment(_thread("это " * 12, 0)) is True

    def test_likes_as_string_parsed(self) -> None:
        assert keep_comment(_thread("short", "9")) is True  # likeCount string -> 9 >= 7

    def test_non_numeric_likes_treated_as_zero(self) -> None:
        assert keep_comment(_thread("short", "lots")) is False

    def test_missing_text_uses_display_fallback(self) -> None:
        assert keep_comment(_thread("b" * 50, 0, use_display=True)) is True

    def test_missing_top_level_comment_dropped(self) -> None:
        thread = {"kind": "youtube#commentThread", "id": "x", "snippet": {"videoId": "v1"}}
        assert keep_comment(thread) is False


class TestCommentExtractors:
    def test_text_prefers_original_over_display(self) -> None:
        assert _comment_text(_thread("orig")) == "orig"

    def test_text_strips_whitespace(self) -> None:
        assert _comment_text(_thread("  hi  ")) == "hi"

    def test_text_missing_returns_empty(self) -> None:
        assert _comment_text({"snippet": {"topLevelComment": {"snippet": {}}}}) == ""

    def test_likes_missing_is_zero(self) -> None:
        assert _comment_likes({"snippet": {"topLevelComment": {"snippet": {}}}}) == 0

    def test_likes_int_passthrough(self) -> None:
        assert _comment_likes(_thread("x", 12)) == 12


class TestHarvestFiltersJunk:
    async def test_fetch_drops_low_quality_comments(self) -> None:
        """End-to-end: junk threads are dropped before storage; good ones kept."""
        good = "this is a substantive complaint about the device failing constantly"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                return httpx.Response(
                    200,
                    json={"items": [{"id": {"kind": "youtube#video", "videoId": "v1"}}]},
                )
            if "/videos?" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"kind": "youtube#video", "id": "v1", "snippet": {},
                             "statistics": {"viewCount": "1000"}}
                        ]
                    },
                )
            if "/commentThreads?" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            _thread("\U0001f605", 0, tid="junk-emoji"),
                            _thread("WOW", 0, tid="junk-short"),
                            _thread(good, 0, tid="keep-long"),
                            _thread("short but liked", 9, tid="keep-liked"),
                        ]
                    },
                )
            return httpx.Response(404)

        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query()]})
            comment_ids = {
                r.external_id for r in records if r.body.get("kind") == "youtube#commentThread"
            }
            assert comment_ids == {"keep-long", "keep-liked"}
        finally:
            await src.aclose()


class TestLogging:
    async def test_per_call_log_redacts_key_and_carries_fields(self) -> None:
        """Per-call log line carries kind, redacted url, count; the raw
        API key never appears in ANY captured log line."""
        captured_extra: list[dict[str, Any]] = []
        captured_text: list[str] = []

        def sink(message: Any) -> None:
            captured_extra.append(dict(message.record["extra"]))
            captured_text.append(str(message))

        sink_id = logger.add(sink, level="DEBUG")
        try:
            handler = _routing_handler(search_pages={"why": ["v1"]}, stats={"v1": "5"})
            src = _src(handler)
            try:
                await src.fetch({"queries": [_search_query(query="why I quit")]})
            finally:
                await src.aclose()

            # Every HTTP step logs one per-call line carrying kind, redacted
            # url, status, elapsed_ms, count -- parity with reddit/hackernews.
            kinds = {c.get("kind") for c in captured_extra if "url" in c}
            assert {"search", "enrich", "comments"} <= kinds, f"captured: {captured_extra}"
            for kind in ("search", "enrich", "comments"):
                log = next(c for c in captured_extra if c.get("kind") == kind)
                assert log["status"] == 200
                assert log["elapsed_ms"] >= 0
                assert "count" in log
                assert "key=REDACTED" in log["url"]
                assert _KEY not in log["url"]
            # The raw key must appear in NO captured log line (extra or text).
            assert all(_KEY not in str(c) for c in captured_extra)
            assert all(_KEY not in text for text in captured_text)
        finally:
            logger.remove(sink_id)
