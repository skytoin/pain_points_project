"""Wave 1 orchestration for YouTube.

Bridges Wave 0 (`JobPlan.youtube_queries`) and the YouTube adapter's
fetch-params dict. Mechanical rules live here in tested Python: the
RFC 3339 publishedAfter floor from JobSpec.time_window, dedup, the
MAX_YT_QUERIES cap. No token decomposition (YouTube is full-text, not
token-AND). Falls back to a deterministic pain-shaped template when
job_plan is null/invalid. See
`docs/specs/2026-05-22-youtube-source-design.md` sections 9-10.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job, Task
from discovery.hashing import hash_params
from discovery.jobs import JobSpec
from discovery.llm.schemas import JobPlan, YouTubeQuerySpec

MAX_YT_QUERIES: int = 10

_TIME_WINDOW_SECONDS: dict[str, int] = {
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 30 * 86_400,
    "year": 365 * 86_400,
}


def _time_window_rfc3339(time_window: str, as_of: date) -> str | None:
    """Unix-window floor as an RFC 3339 'YYYY-MM-DDTHH:MM:SSZ' string,
    anchored at `as_of` midnight UTC. `all` -> None (omit publishedAfter).

    Offset table is identical to `orchestrator.hackernews._time_window_epoch`
    but emits a string instead of a unix int (YouTube publishedAfter is RFC
    3339, not a numeric filter).
    """
    if time_window == "all":
        return None
    if time_window not in _TIME_WINDOW_SECONDS:
        raise ValueError(f"unknown time window: {time_window!r}")
    anchor = datetime.combine(as_of, time.min, tzinfo=UTC)
    floor = anchor - timedelta(seconds=_TIME_WINDOW_SECONDS[time_window])
    return floor.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_query(query: str) -> str:
    """Collapse internal whitespace and strip leading/trailing space."""
    return " ".join(query.split())


def _build_fetch_params(query: str, published_after: str | None) -> dict[str, Any]:
    """Assemble the per-query dict the YouTube adapter consumes (spec SS10)."""
    return {
        "query": query,
        "order": "relevance",
        "type": "video",
        "part": "snippet",
        "published_after": published_after,
        "max_results": 50,
    }


def _compile_yt_queries(
    specs: Iterable[YouTubeQuerySpec], job_spec: JobSpec
) -> list[dict[str, Any]]:
    """Normalize -> dedup (case-insensitive) -> publishedAfter -> cap.
    Preserves the LLM's emission order (a ranking signal). No token
    decomposition (YouTube is full-text relevance, not token-AND).
    """
    published_after = _time_window_rfc3339(job_spec.time_window, job_spec.as_of)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for spec in specs:
        query = _normalize_query(spec.query)
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_build_fetch_params(query, published_after))
        if len(out) >= MAX_YT_QUERIES:
            break
    return out
