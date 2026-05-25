"""YouTube source adapter via the YouTube Data API v3.

See `.claude/skills/source-adapter/SKILL.md` for the umbrella contract,
`.claude/skills/youtube-source/SKILL.md` for the YouTube operational
rules, and `docs/specs/2026-05-22-youtube-source-design.md` for the
design. Quota (search.list = 100 units of 10,000/day) is the harshest
constraint; the adapter is built to be stingy with search and free with
the 1-unit enrichment + comment calls.

Three-step fetch: search.list -> videos.list (stats enrichment) ->
commentThreads.list (top COMMENT_TOP_K videos by view count). Quota-
aware retry: retry transient/rate-limit with backoff; hard-stop on
quotaExceeded (never retry into the wall).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from discovery.sources.base import RawRecord

_API_BASE = "https://www.googleapis.com/youtube/v3"


def build_search_url(query: dict[str, Any], api_key: str) -> str:
    """Build a search.list URL. `published_after` is omitted when None
    (time_window='all'). YouTube q is full-text (not token-AND)."""
    params: dict[str, str] = {
        "part": query["part"],
        "q": query["query"],
        "type": query["type"],
        "order": query["order"],
        "maxResults": str(query["max_results"]),
        "key": api_key,
    }
    published_after = query.get("published_after")
    if published_after is not None:
        params["publishedAfter"] = published_after
    return f"{_API_BASE}/search?{urlencode(params)}"


def build_videos_url(video_ids: list[str], api_key: str) -> str:
    """Build a videos.list URL for up to 50 ids (caller batches)."""
    params = {
        "part": "snippet,statistics",
        "id": ",".join(video_ids),
        "key": api_key,
    }
    return f"{_API_BASE}/videos?{urlencode(params)}"


def build_comments_url(video_id: str, api_key: str) -> str:
    """Build a commentThreads.list URL (top 100 relevance-ranked)."""
    params = {
        "part": "snippet",
        "videoId": video_id,
        "order": "relevance",
        "maxResults": "100",
        "key": api_key,
    }
    return f"{_API_BASE}/commentThreads?{urlencode(params)}"


def extract_video_ids(search_payload: dict[str, Any]) -> list[str]:
    """Pull videoIds from a search.list payload, skipping non-video items
    (defensive even with type=video)."""
    out: list[str] = []
    for item in search_payload.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if vid:
            out.append(str(vid))
    return out


def video_to_raw_record(video: dict[str, Any]) -> RawRecord:
    """videos.list resource -> RawRecord. Verbatim (kind=youtube#video)."""
    return RawRecord(source="youtube", external_id=str(video["id"]), body=video)


def comment_to_raw_record(thread: dict[str, Any]) -> RawRecord:
    """commentThreads.list resource -> RawRecord. Verbatim; body carries
    snippet.videoId so the video link survives (kind=youtube#commentThread)."""
    return RawRecord(source="youtube", external_id=str(thread["id"]), body=thread)


def search_hit_to_raw_record(item: dict[str, Any]) -> RawRecord:
    """Fallback only (enrichment quota-stop): store the search item
    verbatim (kind=youtube#searchResult)."""
    return RawRecord(source="youtube", external_id=str(item["id"]["videoId"]), body=item)


def viewcount_of(video: dict[str, Any]) -> int:
    """Parse statistics.viewCount (a string) to int; 0 when absent
    (live/upcoming videos can lack it -> they sort last for comments)."""
    raw = video.get("statistics", {}).get("viewCount")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
