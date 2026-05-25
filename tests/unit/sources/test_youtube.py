from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from aiolimiter import AsyncLimiter

from discovery.sources.youtube import (
    build_comments_url,
    build_search_url,
    build_videos_url,
    comment_to_raw_record,
    extract_video_ids,
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
