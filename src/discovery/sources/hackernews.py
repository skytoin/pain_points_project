"""HackerNews source adapter via the Algolia HN Search API.

See `.claude/skills/source-adapter/SKILL.md` for the umbrella contract
and `docs/specs/2026-05-20-hackernews-source-design.md` for the HN-
specific design. Once the `hackernews-source` project skill lands in
Chunk 5, it becomes the operational reference for this file.

This module grows in three tasks (Chunk 2):

1. `build_search_url` -- pure URL builder for both Algolia endpoints.
2. `keep_hit`, `hit_to_raw_record` -- pure hit conversion helpers.
3. `HackerNewsSource(BaseSource)` -- the adapter class.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.base import BaseSource, RawRecord

_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"


def build_search_url(query: dict[str, Any]) -> str:
    """Build an Algolia HN Search URL from a compiled query spec.

    Required keys in `query`:

    - `endpoint`        -- `"search"` or `"search_by_date"`
    - `query`           -- full-text search string (already
      decomposed to <=2 content tokens by the orchestrator)
    - `tags`            -- Algolia tag filter (`"story"` or `"show_hn"`)
    - `numeric_filters` -- comma-AND filter string (e.g.
      `"created_at_i>1715040000,points>5,num_comments>3"`)
    - `hits_per_page`   -- int; the orchestrator sets this to 30 (no-
      pagination policy, spec §11)

    The output URL ALWAYS pins `page=0` (Algolia's first page). The
    caller must not pass a `page` key -- the no-pagination policy is
    enforced here, not deferred to Algolia's default, per spec §11.
    """
    endpoint = query["endpoint"]
    if endpoint not in ("search", "search_by_date"):
        raise ValueError(f"unknown HN endpoint: {endpoint!r}")
    params = {
        "query": query["query"],
        "tags": query["tags"],
        "numericFilters": query["numeric_filters"],
        "hitsPerPage": str(query["hits_per_page"]),
        "page": "0",
    }
    return f"{_ALGOLIA_BASE}/{endpoint}?{urlencode(params)}"


def keep_hit(hit: dict[str, Any]) -> bool:
    """Adapter-side floor -- near-noop. Server-side `numericFilters`
    does the quality work (spec §11). Locally we only drop hits with
    no `objectID` (impossible per Algolia's docs but cheap defense).
    """
    return hit.get("objectID") is not None


def hit_to_raw_record(hit: dict[str, Any]) -> RawRecord:
    """Convert an Algolia HN hit into a `RawRecord`.

    - `external_id = str(hit["objectID"])` -- HN's permanent story id,
      always present per Algolia's index.
    - `body = hit` verbatim -- Wave 2 parses; spec §3 "Bronze stores
      raw" is a locked decision.
    - No snippet construction, no permalink fallback, no body trimming.
      Those are Wave 2 concerns in this project, even though the HN
      guide discusses them adapter-side.
    """
    return RawRecord(
        source="hackernews",
        external_id=str(hit["objectID"]),
        body=hit,
    )


class HackerNewsSource(BaseSource):
    """HN source adapter via the Algolia HN Search API.

    No auth, no User-Agent requirement, generous rate limits.

    Constructor parameters
    ----------------------
    client :
        Optional pre-built `httpx.AsyncClient`. If omitted, a fresh one
        is created. Tests inject a client backed by `httpx.MockTransport`.
    limiter :
        Optional `AsyncLimiter`. Default = a fresh per-instance limiter
        (5 req/s polite). Per-instance, NOT a process-wide singleton:
        only one HN consumer exists in this project, unlike Reddit
        which has two sharing a 10/min budget (spec §11).
    timeout :
        httpx client timeout when we create the client ourselves.

    No retry -- see spec §11 / §16. One GET per query; non-2xx or
    network errors are recorded per-query and the loop continues
    (partial success). When every query fails, the first error is
    re-raised so the worker can mark the task failed.
    """

    name = "hackernews"
    rate_limit = (5, 1)  # 5 req/s polite -- Algolia ceiling is ~10k/hr

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        self._owned_client = client is None
        self._limiter = limiter if limiter is not None else AsyncLimiter(max_rate=5, time_period=1)

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        """Run every query in `params['queries']` and collect results.

        Partial success per spec §11: a failed query does not poison
        the others. If every query fails, the first error is re-raised.
        No retry (locked divergence from the source-adapter umbrella).
        """
        records: list[RawRecord] = []
        errors: list[Exception] = []
        for q in params.get("queries", []):
            try:
                page_records = await self._run_one(q)
                records.extend(page_records)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("hn query failed", query=q, error=str(exc))
                errors.append(exc)

        if not records and errors:
            raise errors[0]
        return records

    async def _run_one(self, query: dict[str, Any]) -> list[RawRecord]:
        url = build_search_url(query)
        started_at = time.monotonic()
        async with self._limiter:
            response = await self._client.get(url)
        elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
        response.raise_for_status()
        payload = response.json()
        hits = payload.get("hits", [])

        out = [hit_to_raw_record(hit) for hit in hits if keep_hit(hit)]

        # Skill item 21 analog -- per-query diagnostic line.
        logger.info(
            "hn query done",
            url=url,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            count_before_filter=len(hits),
            count_after_filter=len(out),
            endpoint=query.get("endpoint"),
            tags=query.get("tags"),
        )
        return out

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we created it."""
        if self._owned_client:
            await self._client.aclose()
