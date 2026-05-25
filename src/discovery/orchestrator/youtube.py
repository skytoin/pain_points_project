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
    "month": 30 * 86_400,  # 2,592,000
    "year": 365 * 86_400,  # 31,536,000
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
    """Whitespace-only normalization: collapse internal whitespace runs and
    strip leading/trailing space. Does NOT lowercase -- the emitted query is
    sent near-verbatim to YouTube. Case-insensitive dedup happens separately
    in `_compile_yt_queries` via a lowercased dedup key, leaving the original
    casing intact in the output."""
    return " ".join(query.split())


def _build_fetch_params(query: str, published_after: str | None) -> dict[str, Any]:
    """Assemble the per-query dict the YouTube adapter consumes (spec §10)."""
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


def youtube_queries_for_spec(spec: JobSpec) -> list[dict[str, Any]]:
    """Deterministic no-LLM fallback -- pain-shaped phrases off the
    industry literal. Used when job_plan is null/invalid. Same compile
    path as the LLM output. Mirrors hn_keyword_candidates_for_spec.
    """
    industry = spec.industry
    candidates = [
        YouTubeQuerySpec(
            query=f"why I quit {industry}",
            intent="complaint",
            rationale="(template) quit-the-industry pain monologue",
        ),
        YouTubeQuerySpec(
            query=f"{industry} horror stories",
            intent="complaint",
            rationale="(template) compiled pain across many people",
        ),
        YouTubeQuerySpec(
            query=f"things nobody tells you about {industry}",
            intent="complaint",
            rationale="(template) retrospective pain",
        ),
        YouTubeQuerySpec(
            query=f"{industry} tutorial",
            intent="discussion",
            rationale="(template) comments hold this breaks for me pain",
        ),
        YouTubeQuerySpec(
            query=f"day in the life {industry}",
            intent="discussion",
            rationale="(template) visible workflow friction",
        ),
    ]
    return _compile_yt_queries(candidates, spec)


def _queries_from_job_plan(job: Job) -> list[dict[str, Any]] | None:
    """Extract compiled YouTube queries from a populated `job_plan`, or
    return None to signal 'use the template instead.'

    Returns:
    - `None`  when `job.job_plan` is null OR fails JobPlan validation
      (template fallback signal).
    - `[]`    when `job_plan` is valid but `youtube_queries` is empty (LLM
      intentionally emitted nothing -- graceful sparsity; do NOT fall
      back to template).
    - `[...]` when `youtube_queries` is non-empty (compile pipeline applied).
    """
    if job.job_plan is None:
        return None
    try:
        plan = JobPlan.model_validate(job.job_plan)
    except Exception as e:
        logger.warning(
            "job {} has a job_plan that fails validation ({}); falling back to YouTube template.",
            job.id,
            e,
        )
        return None
    spec = JobSpec.model_validate(job.spec)
    return _compile_yt_queries(plan.youtube_queries, spec)


async def enqueue_youtube_task_for_job(session: AsyncSession, job: Job) -> Task:
    """Queue one YouTube fetch task for `job`. Idempotent on `content_hash`.

    Query source priority:

    1. `job.job_plan["youtube_queries"]` (Wave 0 LLM output), compiled.
    2. `youtube_queries_for_spec(spec)` -- the deterministic template --
       when Wave 0 didn't run or its plan failed validation.

    An empty compiled list is intentional (graceful YouTube sparsity) and
    DOES enqueue a task; the task runs, fetches zero records, and completes
    `done`. Mirrors `orchestrator.hackernews.enqueue_hn_task_for_job`.
    """
    spec = JobSpec.model_validate(job.spec)
    queries = _queries_from_job_plan(job)
    if queries is None:
        # Note: `is None`, not `or` -- an empty `youtube_queries` from the LLM
        # is a valid output (graceful sparsity); only a missing or invalid
        # job_plan triggers the template fallback.
        queries = youtube_queries_for_spec(spec)
    params: dict[str, Any] = {"queries": queries}
    content_hash = hash_params({"source": "youtube", "action": "fetch", "params": params})

    existing = await session.exec(
        select(Task).where(
            Task.job_id == job.id,
            Task.content_hash == content_hash,
        )
    )
    task = existing.first()
    if task is not None:
        return task

    task = Task(
        job_id=job.id,
        wave=1,
        source="youtube",
        action="fetch",
        params=params,
        content_hash=content_hash,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task
