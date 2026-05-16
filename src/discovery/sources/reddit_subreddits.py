"""Reddit subreddit-discovery client and its DTOs.

NOT a `BaseSource`. This hits Reddit's `/subreddits/search.json`
endpoint to find *real, currently-existing* subreddits for Wave 0
query planning. The result is a planning artifact, never Bronze
`raw_records` data — so it returns `SubredditCandidate` DTOs, not
`RawRecord`s. It still obeys the source-adapter contract (async httpx,
shared rate limiter, retry, Pydantic-validated response) and the
reddit-source skill (User-Agent, 6.1s pacing, 401/403 raise, partial
success, per-request logging).

See `.claude/skills/reddit-source/SKILL.md` (items 2,3,4,10,17,20,21)
and `docs/specs/2026-05-15-subreddit-discovery-design.md`.

This module holds the planning DTOs (`SubredditCandidate`, `PhraseResult`),
the pure helpers (`clean_description`, `render_candidate_table`), and the
async `search_subreddits` client that produces them.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from discovery.sources.reddit_ratelimit import get_reddit_limiter

_DESCRIPTION_LIMIT = 300

# The 6 columns the LLM (Call #2) sees, in order. `subreddit_type` and
# `over18` are carried on the DTO for deterministic filtering only and
# are intentionally NOT in this projection (spec §5).
_TABLE_COLUMNS: tuple[str, ...] = (
    "name",
    "subscribers",
    "active_user_count",
    "activity_ratio",
    "public_description",
    "matched_phrases",
)


def clean_description(raw: str) -> str:
    """Collapse whitespace runs and truncate to ~300 chars.

    `public_description` is the LLM's primary relevance signal (spec
    §6); a few hundred chars is plenty and keeps the rendered table
    compact (spec §5: 25 raw t5 objects ≈ 80k tokens, the projection a
    few hundred).
    """
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if len(collapsed) <= _DESCRIPTION_LIMIT:
        return collapsed
    return collapsed[:_DESCRIPTION_LIMIT] + "…"


class SubredditCandidate(BaseModel):
    """One deduped, surviving subreddit considered for Wave 0 selection.

    Six fields are projected into the table the LLM sees;
    `subreddit_type`/`over18` are filter-only and dropped before the LLM
    (spec §5). `matched_phrases` and `activity_ratio` are populated by
    the deterministic pipeline, not at client parse time.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    subscribers: int = 0
    active_user_count: int = 0
    activity_ratio: float = 0.0
    public_description: str = ""
    matched_phrases: int = 0
    subreddit_type: str = "public"
    over18: bool = False


class PhraseResult(BaseModel):
    """Raw per-phrase search result. One entry per phrase request that
    succeeded (failed phrases omitted — partial success, skill item 17).
    Candidates carry raw fields only; the pipeline sets `matched_phrases`
    and `activity_ratio` later.
    """

    model_config = ConfigDict(frozen=True)

    phrase: str
    candidates: list[SubredditCandidate] = Field(default_factory=list)


def render_candidate_table(candidates: list[SubredditCandidate]) -> str:
    """Render candidates as a compact tab-delimited table — header line
    plus one row per subreddit, exactly the 6 columns in
    `_TABLE_COLUMNS` (spec §5). NOT raw JSON: compaction is mandatory,
    not an optimization. Tabs/newlines inside the description are
    replaced with spaces so the column count stays exactly 6.
    """
    lines = ["\t".join(_TABLE_COLUMNS)]
    for c in candidates:
        desc = c.public_description.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        lines.append(
            "\t".join(
                [
                    c.name,
                    str(c.subscribers),
                    str(c.active_user_count),
                    str(c.activity_ratio),
                    desc,
                    str(c.matched_phrases),
                ]
            )
        )
    return "\n".join(lines)


_SUBREDDITS_SEARCH_URL = "https://www.reddit.com/subreddits/search.json"
_MAX_BACKOFF = 300.0


class _SubredditT5(BaseModel):
    """Minimal validated view of one Reddit `t5` object. Only the fields
    we need (source-adapter contract: validate the shape you got).
    `active_user_count` and `accounts_active` are two spellings of the
    same signal; prefer the former, fall back to the latter, default 0.
    """

    model_config = ConfigDict(extra="ignore")

    display_name: str
    subscribers: int | None = 0
    active_user_count: int | None = None
    accounts_active: int | None = None
    subreddit_type: str | None = "public"
    over18: bool | None = False
    public_description: str | None = ""

    def to_candidate(self) -> SubredditCandidate:
        active = (
            self.active_user_count
            if self.active_user_count is not None
            else (self.accounts_active or 0)
        )
        return SubredditCandidate(
            name=self.display_name,
            subscribers=self.subscribers or 0,
            active_user_count=active or 0,
            public_description=clean_description(self.public_description or ""),
            subreddit_type=self.subreddit_type or "public",
            over18=bool(self.over18),
        )


def _build_url(phrase: str) -> str:
    """`/subreddits/search.json` with the spec §7-step-1 params. `sort`
    is omitted — Reddit's sub-search `sort` is non-functional (spec §1);
    all ranking is ours.
    """
    params = {
        "q": phrase,
        "limit": "100",
        "raw_json": "1",
        "include_over_18": "false",
    }
    return f"{_SUBREDDITS_SEARCH_URL}?{urlencode(params)}"


def _backoff_seconds(attempt: int) -> float:
    """5s, 10s, 20s, capped at 300s. `2.0 ** attempt` keeps the result
    unambiguously float (mirrors reddit.py)."""
    return min(5.0 * (2.0**attempt), _MAX_BACKOFF)


def _retry_after_or_backoff(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            wait = float(retry_after)
        except ValueError:
            wait = _backoff_seconds(attempt)
    else:
        wait = _backoff_seconds(attempt)
    return max(1.0, min(wait, _MAX_BACKOFF))  # clamp 1s..5min (skill item 4)


async def _get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    user_agent: str,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
) -> httpx.Response:
    """Mirror of reddit.py's retry policy (skill item 4), with the
    mandatory difference that 401/403 RAISE before results are
    interpreted (spec §11 item 4): a 403 silently mapped to empty would
    be indistinguishable from a legitimate empty search (skill item 20).

    - 401/403 → raise immediately (auth/IP block; no retry).
    - 429 → retry, honour Retry-After (clamped 1s..5min).
    - 5xx / network → retry with exponential backoff.
    - other 4xx (404/414/…) → raise (unexpected; surface it).
    """
    last_exc: httpx.HTTPError | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(url, headers={"User-Agent": user_agent})
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < max_retries:
                await sleep(_backoff_seconds(attempt))
                continue
            raise

        if response.status_code in (401, 403):
            response.raise_for_status()

        if response.status_code == 429:
            if attempt >= max_retries:
                response.raise_for_status()
            await sleep(_retry_after_or_backoff(response, attempt))
            continue

        if 500 <= response.status_code < 600:
            if attempt >= max_retries:
                response.raise_for_status()
            await sleep(_backoff_seconds(attempt))
            continue

        response.raise_for_status()  # any other 4xx → raise (spec §11.4)
        return response

    assert last_exc is not None  # unreachable; typecheck-friendly
    raise last_exc


def _parse_listing(
    payload: dict[str, object],
) -> tuple[list[SubredditCandidate], int]:
    """Parse a Reddit listing into candidates plus the raw pre-validation
    child count. `children` may be absent, null, or a non-list on a
    malformed response — all coerced to an empty list so a bad payload
    cannot abort the batch (the count is then 0).
    """
    data = payload.get("data") or {}
    children = data.get("children") or [] if isinstance(data, dict) else []
    if not isinstance(children, list):
        children = []
    out: list[SubredditCandidate] = []
    for child in children:
        raw = child.get("data", {}) if isinstance(child, dict) else {}
        try:
            out.append(_SubredditT5.model_validate(raw).to_candidate())
        except ValidationError:
            logger.debug("skipping malformed t5 object", raw=raw)
    return out, len(children)


async def _search_one_phrase(
    http: httpx.AsyncClient,
    phrase: str,
    *,
    lim: AsyncLimiter,
    user_agent: str,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
) -> PhraseResult:
    """One rate-limited `/subreddits/search.json` request for `phrase`,
    plus the skill-item-21 per-request log. Raising paths (HTTP/JSON)
    propagate to the caller's partial-success handler.
    """
    url = _build_url(phrase)
    started = time.monotonic()
    async with lim:
        response = await _get_with_retries(
            http, url, user_agent=user_agent, sleep=sleep, max_retries=max_retries
        )
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    payload = response.json()
    candidates, raw_count = _parse_listing(payload)
    logger.info(
        "subreddit search done",
        url=url,
        status=response.status_code,
        elapsed_ms=elapsed_ms,
        count_before_filter=raw_count,
        count_after_filter=len(candidates),
        phrase=phrase,
    )
    return PhraseResult(phrase=phrase, candidates=candidates)


async def search_subreddits(
    phrases: list[str],
    *,
    user_agent: str,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: AsyncLimiter | None = None,
    max_retries: int = 3,
    http_timeout: float = 30.0,
) -> list[PhraseResult]:
    """One `/subreddits/search.json` request per phrase (spec §7 step 1).

    Partial success (skill item 17): a phrase failing after retries does
    not kill the others. Only a TOTAL wipeout (every phrase failed)
    raises (the first error, so the station maps it to
    `QueryExpansionError`). Empty children = `ok_empty`, not failure
    (skill item 20). Shares the process-wide Reddit limiter (spec §11
    item 3) unless one is injected.
    """
    own_client = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=http_timeout)
    lim = limiter if limiter is not None else get_reddit_limiter()

    results: list[PhraseResult] = []
    errors: list[Exception] = []
    try:
        for phrase in phrases:
            try:
                results.append(
                    await _search_one_phrase(
                        http,
                        phrase,
                        lim=lim,
                        user_agent=user_agent,
                        sleep=sleep,
                        max_retries=max_retries,
                    )
                )
            # json.JSONDecodeError is a ValueError (response.json() decode)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("subreddit search failed", phrase=phrase, error=str(exc))
                errors.append(exc)
    finally:
        if own_client:
            await http.aclose()

    if not results and errors:
        raise errors[0]
    return results
