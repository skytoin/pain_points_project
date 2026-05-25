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

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter

from discovery.sources.base import BaseSource, RawRecord

_API_BASE = "https://www.googleapis.com/youtube/v3"

# --- constants ----------------------------------------------------------
COMMENT_TOP_K = 50  # videos to harvest comments from, by view count
VIDEOS_BATCH = 50  # max ids per videos.list call
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


# --- exceptions ---------------------------------------------------------
# Each carries an N818 suppression: the names are spec-locked (the design
# doc and tests reference them by their reason-shaped names, not an Error
# suffix), so the pep8-naming "Error suffix" rule is intentionally waived.
class YouTubeQuotaExceeded(Exception):  # noqa: N818
    """Daily quota gone (403 quotaExceeded/dailyLimitExceeded). Terminal:
    never retried; the caller stops cleanly and keeps partial results."""


class YouTubeRateLimited(Exception):  # noqa: N818
    """Too-fast (403/429 rateLimitExceeded). Transient: retried."""


class CommentsDisabled(Exception):  # noqa: N818
    """commentThreads.list 403 commentsDisabled. Per-video skip."""


def _reason_of(response: httpx.Response) -> str | None:
    """Read error.errors[0].reason from a JSON error body; None if absent."""
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return None
    return errors[0].get("reason") if errors else None


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


class YouTubeSource(BaseSource):
    """YouTube Data API v3 adapter (three-step fetch, quota-aware retry).

    Constructor mirrors RedditSource/HackerNewsSource, plus an `api_key`
    and an injectable `sleep` (so backoff is exercised in tests without
    real waits under `filterwarnings=["error"]`). When `api_key` is None
    the adapter no-ops (returns []) with zero HTTP calls. The limiter is
    per-instance (one consumer); quota, not rate, is the real ceiling.
    """

    name = "youtube"
    rate_limit = (5, 1)  # 5 req/s polite; quota (not rate) is the real ceiling

    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        self._owned_client = client is None
        self._limiter = limiter if limiter is not None else AsyncLimiter(max_rate=5, time_period=1)
        self._sleep = sleep
        self._max_retries = max_retries

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        """Three-step fetch. Fully implemented in Task 2.4; this shell
        satisfies the BaseSource abstract method for Task 2.3."""
        raise NotImplementedError(str(params))  # pragma: no cover

    async def _get_json(self, url: str) -> dict[str, Any]:
        """GET with quota-aware retry. Classifies 403 reasons BEFORE the
        retry decision: quota/commentsDisabled raise immediately; rate-
        limit + 5xx + network errors retry with backoff (5s,10s,20s cap
        300s). Mirrors RedditSource._fetch_with_retries."""
        for attempt in range(self._max_retries + 1):
            try:
                async with self._limiter:
                    response = await self._client.get(url)
            except httpx.HTTPError:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_seconds(attempt))
                    continue
                raise
            retry = self._classify(response, attempt)
            if retry is None:
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            await self._sleep(retry)
        raise RuntimeError("unreachable: retry loop exited")  # pragma: no cover

    def _classify(self, response: httpx.Response, attempt: int) -> float | None:
        """Return a backoff delay to retry, or None to accept/raise. Raises
        the terminal exceptions directly."""
        if response.status_code == 403:
            reason = _reason_of(response)
            if reason in _QUOTA_REASONS:
                raise YouTubeQuotaExceeded(reason or "quotaExceeded")
            if reason == "commentsDisabled":
                raise CommentsDisabled("commentsDisabled")
            if reason in _RATE_REASONS:
                if attempt < self._max_retries:
                    return self._backoff_seconds(attempt)
                raise YouTubeRateLimited(reason or "rateLimitExceeded")
            return None  # other 403 -> non-retryable, raise_for_status handles it
        if 500 <= response.status_code < 600 and attempt < self._max_retries:
            return self._backoff_seconds(attempt)
        return None

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """5s, 10s, 20s, capped at 300s. `2.0 ** attempt` keeps the result
        type unambiguously float (mirrors RedditSource)."""
        return min(5.0 * (2.0**attempt), 300.0)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we created it."""
        if self._owned_client:
            await self._client.aclose()
