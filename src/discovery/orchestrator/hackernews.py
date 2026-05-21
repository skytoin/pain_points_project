"""Wave 1 orchestration for HackerNews.

Bridges the Wave 0 LLM output (`JobPlan.hn_queries`) and the HN adapter's
fetch-params dict. Every brittle mechanical rule from the design spec
lives here in tested Python:

- Token decomposition (delegated to `discovery.sources.keyword_tokens`).
- Endpoint + tag routing from the LLM's per-candidate `intent` flag.
- Server-side `numericFilters` from `JobSpec.time_window` and `as_of`.
- The `MAX_HN_QUERIES=6` cap.

When `Job.job_plan` is null (Wave 0 failed) or fails validation, falls
back to the deterministic capability-first template so HN keeps working
with `OPENAI_API_KEY` unset -- mirroring Reddit's template fallback.

See `docs/specs/2026-05-20-hackernews-source-design.md` §10.
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
from discovery.llm.schemas import HackerNewsKeywordSpec, JobPlan
from discovery.sources.keyword_tokens import decompose_keyword

_TIME_WINDOW_SECONDS: dict[str, int] = {
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 30 * 86_400,  # 2,592,000
    "year": 365 * 86_400,  # 31,536,000
}

# Routing table -- the deterministic 2:1 launch/context split that
# Python owns. Each entry maps an intent flag to (endpoint, tags,
# extra_numeric_filters). Created_at_i is layered on top by
# `_compile_hn_queries` from the JobSpec time window.
_ROUTING: dict[str, tuple[str, str, list[str]]] = {
    "launch": ("search_by_date", "show_hn", []),
    "context": ("search", "story", ["points>5", "num_comments>3"]),
}


def _time_window_epoch(time_window: str, as_of: date) -> int | None:
    """Compute the unix-seconds floor for `created_at_i` from the job's
    time window, anchored at `as_of` midnight UTC.

    `all` -> None (caller omits `created_at_i` entirely from
    numericFilters; the rest of the filter list still applies).

    `hour | day | week | month | year` -> integer epoch seconds.
    """
    if time_window == "all":
        return None
    if time_window not in _TIME_WINDOW_SECONDS:
        raise ValueError(f"unknown time window: {time_window!r}")
    anchor = datetime.combine(as_of, time.min, tzinfo=UTC)
    floor = anchor - timedelta(seconds=_TIME_WINDOW_SECONDS[time_window])
    return int(floor.timestamp())


def _routing_for(intent: str) -> tuple[str, str, list[str]]:
    """Map an intent flag to (endpoint, tags, extra_numeric_filters).

    Raises KeyError on unknown intent -- the LLM contract is enforced
    by the `HackerNewsKeywordSpec.intent` Literal, so an unknown value
    indicates a contract violation upstream.
    """
    return _ROUTING[intent]


MAX_HN_QUERIES: int = 6


def _build_fetch_params(
    query_tokens: list[str],
    endpoint: str,
    tags: str,
    numeric_filters: str,
) -> dict[str, Any]:
    """Assemble the per-query dict the HN adapter consumes (spec §10)."""
    return {
        "endpoint": endpoint,
        "query": " ".join(query_tokens),
        "tags": tags,
        "numeric_filters": numeric_filters,
        "hits_per_page": 30,
    }


def _compile_hn_queries(
    specs: Iterable[HackerNewsKeywordSpec],
    job_spec: JobSpec,
) -> list[dict[str, Any]]:
    """Decompose -> dedupe -> route -> numericFilters -> cap. Pure
    function. Preserves the LLM's emission order (a ranking signal).
    """
    epoch = _time_window_epoch(job_spec.time_window, job_spec.as_of)
    seen_tokens: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []

    for spec in specs:
        tokens = decompose_keyword(spec.keyword)
        if not tokens:
            continue
        token_key = tuple(tokens)
        if token_key in seen_tokens:
            continue
        seen_tokens.add(token_key)

        endpoint, tags, extra_filters = _routing_for(spec.intent)
        filters: list[str] = []
        if epoch is not None:
            filters.append(f"created_at_i>{epoch}")
        filters.extend(extra_filters)
        numeric_filters = ",".join(filters)

        out.append(_build_fetch_params(tokens, endpoint, tags, numeric_filters))
        if len(out) >= MAX_HN_QUERIES:
            break

    return out


def hn_keyword_candidates_for_spec(spec: JobSpec) -> list[dict[str, Any]]:
    """Deterministic HN fallback -- no LLM. Capability word FIRST so
    decomposition keeps it for multi-word industries (e.g.
    `commercial cleaning CLI` would drop `CLI`; `CLI commercial
    cleaning` keeps `CLI` + the first industry word). Same compile
    path as the LLM output.

    Used when `Job.job_plan` is null (Wave 0 failed or `OPENAI_API_KEY`
    unset). Mirrors `orchestrator.reddit.reddit_queries_for_spec`.
    """
    industry = spec.industry
    candidates = [
        HackerNewsKeywordSpec(
            keyword=f"CLI {industry}",
            intent="launch",
            rationale="(template) CLI launch fallback",
        ),
        HackerNewsKeywordSpec(
            keyword=f"OSS {industry}",
            intent="launch",
            rationale="(template) OSS launch fallback",
        ),
        HackerNewsKeywordSpec(
            keyword=f"API {industry}",
            intent="launch",
            rationale="(template) API launch fallback",
        ),
        HackerNewsKeywordSpec(
            keyword=f"workflow {industry}",
            intent="context",
            rationale="(template) workflow discussion fallback",
        ),
    ]
    return _compile_hn_queries(candidates, spec)


def _queries_from_job_plan(job: Job) -> list[dict[str, Any]] | None:
    """Extract compiled HN queries from a populated `job_plan`, or
    return None to signal "use the template instead."

    Returns:
    - `None`  when `job.job_plan` is null OR fails JobPlan validation
      (template fallback signal).
    - `[]`    when `job_plan` is valid but `hn_queries` is empty (LLM
      intentionally emitted nothing -- graceful sparsity; do NOT fall
      back to template).
    - `[...]` when `hn_queries` is non-empty (compile pipeline applied).
    """
    if job.job_plan is None:
        return None
    try:
        plan = JobPlan.model_validate(job.job_plan)
    except Exception as e:
        logger.warning(
            "job {} has a job_plan that fails validation ({}); falling back to HN template.",
            job.id,
            e,
        )
        return None
    spec = JobSpec.model_validate(job.spec)
    return _compile_hn_queries(plan.hn_queries, spec)


async def enqueue_hn_task_for_job(session: AsyncSession, job: Job) -> Task:
    """Queue one HN fetch task for `job`. Idempotent on `content_hash`.

    Query source priority:

    1. `job.job_plan["hn_queries"]` (Wave 0 LLM output), compiled.
    2. `hn_keyword_candidates_for_spec(spec)` -- the deterministic
       template -- when Wave 0 didn't run or its plan failed
       validation.

    An empty compiled list is intentional (graceful HN sparsity on
    non-tech industries -- spec §17 risk 5) and DOES enqueue a task;
    the task runs, fetches zero records, and completes `done`. Mirrors
    `orchestrator.reddit.enqueue_reddit_task_for_job` for shape.
    """
    spec = JobSpec.model_validate(job.spec)
    queries = _queries_from_job_plan(job)
    if queries is None:
        # Note: `is None`, not `or` -- an empty `hn_queries` from the LLM
        # is a valid output (graceful HN sparsity per spec §17 risk 5);
        # only a missing or invalid job_plan triggers the template fallback.
        queries = hn_keyword_candidates_for_spec(spec)
    params: dict[str, Any] = {"queries": queries}
    content_hash = hash_params({"source": "hackernews", "action": "fetch", "params": params})

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
        source="hackernews",
        action="fetch",
        params=params,
        content_hash=content_hash,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task
