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
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.base import BaseSource, RawRecord

_API_BASE = "https://www.googleapis.com/youtube/v3"

# --- constants ----------------------------------------------------------
COMMENT_TOP_K = 50  # videos to harvest comments from, by view count
VIDEOS_BATCH = 50  # max ids per videos.list call
MIN_COMMENT_CHARS = 45  # keep comment threads with >= this many chars of text...
MIN_COMMENT_LIKES = 7  # ...OR at least this many likes (rescue short-but-upvoted)
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


_KEY_PARAM_RE = re.compile(r"(key=)[^&]*")


def _reason_of(response: httpx.Response) -> str | None:
    """Read error.errors[0].reason from a JSON error body; None if absent."""
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return None
    return errors[0].get("reason") if errors else None


def _redact_key(url: str) -> str:
    """Replace the `key=` query value with REDACTED so the API key never
    appears in any log line."""
    return _KEY_PARAM_RE.sub(r"\1REDACTED", url)


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


def _top_comment_snippet(thread: dict[str, Any]) -> dict[str, Any]:
    snippet = thread.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
    return snippet if isinstance(snippet, dict) else {}


def _comment_text(thread: dict[str, Any]) -> str:
    """Top-level comment text, preferring textOriginal then textDisplay."""
    snippet = _top_comment_snippet(thread)
    text = snippet.get("textOriginal") or snippet.get("textDisplay") or ""
    return str(text).strip()


def _comment_likes(thread: dict[str, Any]) -> int:
    """Top-level comment like count; 0 when absent or non-numeric."""
    raw = _top_comment_snippet(thread).get("likeCount")
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def keep_comment(thread: dict[str, Any]) -> bool:
    """Cheap deterministic quality floor for a commentThread (analog of
    keep_post). Drops emoji/symbol/number-only and short low-engagement
    threads; keeps non-English text. Semantic relevance is Wave 2's job.
    See docs/specs/2026-05-25-youtube-comment-quality-floor.md.
    """
    text = _comment_text(thread)
    if not any(ch.isalpha() for ch in text):  # emoji/symbol/number-only -> drop
        return False
    return len(text) >= MIN_COMMENT_CHARS or _comment_likes(thread) >= MIN_COMMENT_LIKES


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
        """Three-step fetch (search -> enrich -> comments). Thin orchestrator
        over the three named helpers. No-op (zero HTTP calls) when no key."""
        if self._api_key is None:
            logger.warning("youtube: no API key configured; skipping (0 records)")
            return []
        ids, items_by_id = await self._search_all(params.get("queries", []))
        if not ids:
            return []
        video_records, enriched = await self._enrich_videos(ids, items_by_id)
        comment_records = await self._harvest_comments(enriched)
        return video_records + comment_records

    async def _search_all(
        self, queries: list[dict[str, Any]]
    ) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """Run search.list per query. Returns (ordered unique videoIds,
        items_by_id). Stops on quota; partial success otherwise. Raises
        the first error only when nothing was gathered and all errored."""
        ordered: list[str] = []
        items_by_id: dict[str, dict[str, Any]] = {}
        errors: list[Exception] = []
        assert self._api_key is not None
        for q in queries:
            url = build_search_url(q, self._api_key)
            try:
                payload = await self._get_json(url, kind="search")
            except YouTubeQuotaExceeded:
                logger.warning("youtube: quota exhausted during search; stopping early")
                break
            except (YouTubeRateLimited, httpx.HTTPError, ValueError) as exc:
                logger.warning("youtube search failed", query=q, error=str(exc))
                errors.append(exc)
                continue
            for item in payload.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid and vid not in items_by_id:
                    items_by_id[vid] = item
                    ordered.append(str(vid))
        if not ordered and errors:
            raise errors[0]
        return ordered, items_by_id

    async def _enrich_videos(
        self, ids: list[str], items_by_id: dict[str, dict[str, Any]]
    ) -> tuple[list[RawRecord], list[dict[str, Any]]]:
        """Batch ids into videos.list. On quota-stop, emit search-hit
        fallback records for the un-enriched ids."""
        records: list[RawRecord] = []
        enriched: list[dict[str, Any]] = []
        assert self._api_key is not None
        done: set[str] = set()
        for i in range(0, len(ids), VIDEOS_BATCH):
            batch = ids[i : i + VIDEOS_BATCH]
            try:
                payload = await self._get_json(
                    build_videos_url(batch, self._api_key), kind="enrich"
                )
            except YouTubeQuotaExceeded:
                logger.warning("youtube: quota exhausted during enrichment; storing search hits")
                records.extend(
                    search_hit_to_raw_record(items_by_id[vid]) for vid in ids if vid not in done
                )
                return records, enriched
            except YouTubeRateLimited:
                # Transient -- keep what enriched so far; NO search-hit
                # fallback (that is reserved strictly for the quota wall).
                logger.warning("youtube: rate-limited during enrichment; stopping batches")
                break
            for video in payload.get("items", []):
                enriched.append(video)
                records.append(video_to_raw_record(video))
                done.add(str(video.get("id")))
        return records, enriched

    async def _harvest_comments(self, enriched: list[dict[str, Any]]) -> list[RawRecord]:
        """commentThreads.list for the top COMMENT_TOP_K videos by view
        count. Skips commentsDisabled videos; stops on quota."""
        records: list[RawRecord] = []
        assert self._api_key is not None
        ranked = sorted(enriched, key=viewcount_of, reverse=True)[:COMMENT_TOP_K]
        for video in ranked:
            vid = str(video.get("id"))
            try:
                payload = await self._get_json(
                    build_comments_url(vid, self._api_key), kind="comments"
                )
            except CommentsDisabled:
                logger.debug("youtube: comments disabled, skipping", video_id=vid)
                continue
            except YouTubeQuotaExceeded:
                logger.warning("youtube: quota exhausted during comment harvest; stopping")
                break
            except YouTubeRateLimited:
                logger.warning("youtube: rate-limited during comment harvest; stopping")
                break
            threads = payload.get("items", [])
            kept = [t for t in threads if keep_comment(t)]
            records.extend(comment_to_raw_record(t) for t in kept)
            logger.info(
                "youtube comments filtered",
                video_id=vid,
                kept=len(kept),
                dropped=len(threads) - len(kept),
            )
        return records

    async def _get_json(self, url: str, *, kind: str) -> dict[str, Any]:
        """GET with quota-aware retry. Classifies 403/429 reasons BEFORE the
        retry decision: quota/commentsDisabled raise immediately; rate-
        limit + 5xx + network errors retry with backoff (5s,10s,20s cap
        300s). Mirrors RedditSource._fetch_with_retries. Emits one per-call
        diagnostic line (kind, redacted url, status, elapsed_ms, count) on
        the accepted response -- parity with reddit.py / hackernews.py."""
        for attempt in range(self._max_retries + 1):
            started_at = time.monotonic()
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
                self._log_call(kind, url, response, started_at, result)
                return result
            await self._sleep(retry)
        raise RuntimeError("unreachable: retry loop exited")  # pragma: no cover

    @staticmethod
    def _log_call(
        kind: str,
        url: str,
        response: httpx.Response,
        started_at: float,
        payload: dict[str, Any],
    ) -> None:
        """Per-call diagnostic; key redacted (never log the key)."""
        logger.info(
            "youtube call",
            kind=kind,
            url=_redact_key(url),
            status=response.status_code,
            elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
            count=len(payload.get("items", [])),
        )

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
                return self._rate_limit_backoff(attempt, reason)
            return None  # other 403 -> non-retryable, raise_for_status handles it
        if response.status_code == 429:
            # Spec section 4/10: 429 is rate-limit -> retryable with backoff.
            return self._rate_limit_backoff(attempt, _reason_of(response))
        if 500 <= response.status_code < 600 and attempt < self._max_retries:
            return self._backoff_seconds(attempt)
        return None

    def _rate_limit_backoff(self, attempt: int, reason: str | None) -> float:
        """Retry a rate-limited call with backoff, or raise YouTubeRateLimited
        once the retry budget is spent (a transient, partial-success stop)."""
        if attempt < self._max_retries:
            return self._backoff_seconds(attempt)
        raise YouTubeRateLimited(reason or "rateLimitExceeded")

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """5s, 10s, 20s, capped at 300s. `2.0 ** attempt` keeps the result
        type unambiguously float (mirrors RedditSource)."""
        return min(5.0 * (2.0**attempt), 300.0)

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we created it."""
        if self._owned_client:
            await self._client.aclose()
