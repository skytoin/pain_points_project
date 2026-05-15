"""Reddit source adapter — anonymous `.json` endpoint, no OAuth.

See `.claude/skills/source-adapter/SKILL.md` for the adapter contract
and `.claude/skills/reddit-source/SKILL.md` for the operational rules
(rate limits, query budget, search syntax, junk filtering).

This module is split into four pure helpers plus the adapter class so
each piece is testable in isolation:

- `validate_subreddit_name(raw)` — strip `r/`, enforce 3-21 ASCII chars.
- `build_query_url(query)` — pure URL builder for both endpoints.
- `keep_post(post, ...)` — quality floor (score, comments, NSFW, removed).
- `post_to_raw_record(post)` — permalink as natural ID, selftext trimmed.
- `RedditSource.fetch(params)` — orchestrates the above with rate
  limiting, retries, and partial-success collection.
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

_REDDIT_BASE = "https://www.reddit.com"
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{3,21}$")
_BODY_TRIM_LIMIT = 200

# Skill item 3: ~10 requests/min unauthenticated. Use 60.1s to avoid
# bunching at second boundaries.
_DEFAULT_RATE = (10, 60.1)


def validate_subreddit_name(raw: str) -> str | None:
    """Strip a leading `r/` or `/r/` and return the name if it's valid.

    Reddit's rule: 3-21 ASCII letters/digits/underscores. LLMs love to
    produce things like `"Small Business"` or `"AI/ML"` — those become
    404s if sent to Reddit, so we drop them at planning time (skill
    item 10).
    """
    name = raw.strip()
    if name.startswith("/r/"):
        name = name[3:]
    elif name.startswith("r/"):
        name = name[2:]
    if _SUBREDDIT_RE.match(name):
        return name
    return None


def build_query_url(query: dict[str, Any]) -> str:
    """Build a Reddit `.json` URL from a query spec.

    Required keys: `endpoint` (`"per_sub"` or `"site_wide"`), `q`,
    `sort`, `t`, `limit`. `per_sub` also needs `subreddit`.

    Always sets `raw_json=1` (skill item 12 — prevents `&amp;` bugs in
    response text) and `include_over_18=false` (one of two NSFW filters;
    the other belongs in the `q` string itself).
    """
    params = {
        "q": query["q"],
        "sort": query["sort"],
        "t": query["t"],
        "limit": str(query["limit"]),
        "raw_json": "1",
        "include_over_18": "false",
    }
    endpoint = query["endpoint"]
    if endpoint == "per_sub":
        params["restrict_sr"] = "true"
        sub = query["subreddit"]
        return f"{_REDDIT_BASE}/r/{sub}/search.json?{urlencode(params)}"
    if endpoint == "site_wide":
        return f"{_REDDIT_BASE}/search.json?{urlencode(params)}"
    raise ValueError(f"unknown reddit endpoint: {endpoint!r}")


def keep_post(post: dict[str, Any], *, min_score: int, min_comments: int) -> bool:
    """Quality floor — skill item 13. Cheap drop before LLM tokens get spent."""
    if post.get("score", 0) < min_score:
        return False
    if post.get("num_comments", 0) < min_comments:
        return False
    if post.get("over_18", False):
        return False
    if post.get("removed_by_category") is not None:
        return False
    return post.get("author") != "[deleted]"


def _trim_text(text: str, limit: int = _BODY_TRIM_LIMIT) -> str:
    """Collapse whitespace, trim, ellipsis at `limit` — skill item 15."""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def post_to_raw_record(post: dict[str, Any]) -> RawRecord:
    """Convert a Reddit post `data` dict into a `RawRecord`.

    Uses `permalink` as `external_id`, NOT the post's `url` (skill item 14
    — permalinks dedupe per-thread; external URLs collapse cross-sub
    discussions of the same article into one signal).
    """
    body = dict(post)
    selftext = body.get("selftext")
    if isinstance(selftext, str):
        body["selftext"] = _trim_text(selftext)
    return RawRecord(
        source="reddit",
        external_id=post["permalink"],
        body=body,
    )


class RedditSource(BaseSource):
    """Anonymous-`.json`-endpoint Reddit adapter.

    Constructor parameters
    ----------------------
    user_agent :
        Required. Reddit silently throttles generic UA strings (skill
        item 2). Production code wires this from
        `settings.reddit_user_agent`; tests pass an arbitrary string.
    client :
        Optional pre-built `httpx.AsyncClient`. If omitted, a fresh one
        is created. Tests inject a client backed by `httpx.MockTransport`.
    sleep :
        Awaitable sleep function. Tests inject a no-op; production uses
        `asyncio.sleep`. Always pass cancellation through (skill item 18).
    limiter :
        Optional `AsyncLimiter`. Default = 10 req / 60.1s.
    min_score, min_comments :
        Quality-floor thresholds. Defaults from the skill.
    max_retries :
        Retry budget for 429 / 5xx / network errors. Default 3.
    """

    name = "reddit"
    rate_limit = (10, 60)

    def __init__(
        self,
        user_agent: str,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limiter: AsyncLimiter | None = None,
        min_score: int = 5,
        min_comments: int = 2,
        max_retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._user_agent = user_agent
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        self._owned_client = client is None
        self._sleep = sleep
        self._limiter = limiter or AsyncLimiter(_DEFAULT_RATE[0], _DEFAULT_RATE[1])
        self._min_score = min_score
        self._min_comments = min_comments
        self._max_retries = max_retries

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        """Run every query in `params['queries']` and collect results.

        Partial success per skill item 17: a single failed query does
        not poison the others. If *every* query fails, the first error
        is re-raised so the orchestrator can mark the task failed.
        """
        clean = self._clean_queries(params.get("queries", []))

        records: list[RawRecord] = []
        errors: list[Exception] = []
        for q in clean:
            try:
                page = await self._run_one(q)
                records.extend(page)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("reddit query failed", query=q, error=str(exc))
                errors.append(exc)

        if not records and errors:
            raise errors[0]
        return records

    @staticmethod
    def _clean_queries(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop invalid subreddit names silently — skill item 10."""
        cleaned: list[dict[str, Any]] = []
        for q in queries:
            if q["endpoint"] == "per_sub":
                name = validate_subreddit_name(q["subreddit"])
                if name is None:
                    logger.debug("dropping reddit query: invalid subreddit", query=q)
                    continue
                cleaned.append({**q, "subreddit": name})
            else:
                cleaned.append(q)
        return cleaned

    async def _run_one(self, query: dict[str, Any]) -> list[RawRecord]:
        url = build_query_url(query)
        started_at = time.monotonic()
        async with self._limiter:
            response = await self._fetch_with_retries(url)
        elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
        response.raise_for_status()
        payload = response.json()
        children = payload.get("data", {}).get("children", [])

        out: list[RawRecord] = []
        for child in children:
            post = child.get("data", {})
            if keep_post(post, min_score=self._min_score, min_comments=self._min_comments):
                out.append(post_to_raw_record(post))

        # Skill item 21 — per-query diagnostic line. Carries the URL,
        # HTTP status, response time, and counts before AND after the
        # engagement floor so low-yield runs can be debugged without
        # re-running the discovery from scratch.
        logger.info(
            "reddit query done",
            url=url,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            count_before_filter=len(children),
            count_after_filter=len(out),
            endpoint=query.get("endpoint"),
            subreddit=query.get("subreddit"),
        )
        return out

    async def _fetch_with_retries(self, url: str) -> httpx.Response:
        """Single-URL fetch with the retry policy from skill item 4.

        - 401/403 → no retry (auth/IP problem; surface as failure).
        - 429 → retry, honor Retry-After (clamped to 1s..5min).
        - 5xx / network errors → retry with exponential backoff.
        - 2xx/3xx/other 4xx → return as-is.
        """
        last_exc: httpx.HTTPError | None = None
        last_response: httpx.Response | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(url, headers={"User-Agent": self._user_agent})
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_seconds(attempt))
                    continue
                raise

            last_response = response

            if response.status_code == 429:
                if attempt >= self._max_retries:
                    return response
                wait = self._retry_after_or_backoff(response, attempt)
                await self._sleep(wait)
                continue

            if 500 <= response.status_code < 600:
                if attempt >= self._max_retries:
                    return response
                await self._sleep(self._backoff_seconds(attempt))
                continue

            return response

        # Unreachable in practice, but typecheck-friendly.
        if last_response is not None:
            return last_response
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """5s, 10s, 20s, capped at 300s (5 min).

        `2.0 ** attempt` (rather than `2 ** attempt`) keeps the result
        type unambiguously `float` — `int ** int` is `Any` in mypy's
        stubs because negative exponents produce `float` instead of `int`.
        """
        return min(5.0 * (2.0**attempt), 300.0)

    @classmethod
    def _retry_after_or_backoff(cls, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = cls._backoff_seconds(attempt)
        else:
            wait = cls._backoff_seconds(attempt)
        # Clamp 1s..5min — skill item 4.
        return max(1.0, min(wait, 300.0))

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we created it."""
        if self._owned_client:
            await self._client.aclose()
