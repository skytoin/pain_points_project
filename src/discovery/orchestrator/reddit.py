"""Wave 1 orchestration for Reddit.

When Wave 0 (LLM query expansion) succeeds, `Job.job_plan` carries the
LLM-built `RedditQuerySpec` list. This module reads from there first.
When `job_plan` is null (Wave 0 failed or never ran), it falls back to
a hand-rolled deterministic template — baseline business/startup
subreddits crossed with a few pain-phrase categories from the
`reddit-source` skill.

Public surface
--------------
- `reddit_queries_for_spec(spec)` — the deterministic template. Pure
  helper, returns a list of query dicts accepted by `RedditSource.fetch`.
- `enqueue_reddit_task_for_job(session, job)` — idempotent: inserts at
  most one Reddit task per job (UNIQUE on `(job_id, content_hash)`).
  Reads from `job.job_plan["reddit_queries"]` when present; falls
  through to the template when null or corrupted.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job, Task
from discovery.hashing import hash_params
from discovery.jobs import JobSpec
from discovery.llm.schemas import JobPlan, RedditQuerySpec

# Baseline subreddits — not industry-specific, kept short to stay under
# Reddit's ~4 KB URL ceiling (skill item 7). Wave 0 will mix in
# domain-specific subs (e.g. `r/nursing` for the healthcare industry).
_BASELINE_SUBREDDITS: list[str] = [
    "smallbusiness",
    "Entrepreneur",
    "startups",
    "microsaas",
]

# Pain-phrase categories — grouped by meaning, not keyword (skill item 8).
# Variants per category capped at 3 to keep URLs compact and the OR
# expression precise.
_PAIN_PHRASE_CATEGORIES: dict[str, list[str]] = {
    "willingness_to_pay": ['"I would pay"', '"I\'d pay"', '"would pay for"'],
    "unmet_need": ['"wish there was"', '"wish someone would"'],
    "frustration": ['"frustrated with"', '"fed up with"', '"tired of"'],
    "alternative": ['"alternative to"', '"replacement for"'],
}


def reddit_queries_for_spec(spec: JobSpec) -> list[dict[str, Any]]:
    """Build the deterministic Wave 1 Reddit query plan for `spec`.

    One site-wide query per pain-phrase category. Each query OR-compresses
    the baseline subreddits with the variants for that category, anchored
    on the industry literal (quoted, so Reddit treats it as a phrase).
    """
    sub_clause = " OR ".join(f"subreddit:{name}" for name in _BASELINE_SUBREDDITS)
    industry_clause = f'"{spec.industry}"'

    return [
        {
            "endpoint": "site_wide",
            "q": f"({sub_clause}) AND {industry_clause} AND ({' OR '.join(phrases)})",
            "sort": "top",
            "t": "month",
            "limit": 100,
        }
        for phrases in _PAIN_PHRASE_CATEGORIES.values()
    ]


def _queries_from_job_plan(job: Job) -> list[dict[str, Any]] | None:
    """Extract Reddit queries from a populated `job_plan`, or None.

    Returns None when `job_plan` is null (Wave 0 hasn't run or failed)
    OR when validation of the stored dict fails for any reason. Caller
    falls back to the deterministic template in either case.
    """
    if job.job_plan is None:
        return None
    try:
        plan = JobPlan.model_validate(job.job_plan)
    except Exception as e:
        logger.warning(
            "job {} has a job_plan that fails validation ({}); falling back to template.",
            job.id,
            e,
        )
        return None
    return [_compile_query(q) for q in plan.reddit_queries]


def _compile_query(spec: RedditQuerySpec) -> dict[str, Any]:
    """Compile a `RedditQuerySpec` into the dict shape `RedditSource.fetch`
    accepts. The LLM has already filled `q`; validation has already
    dropped invalid ones; this is just shape conversion.
    """
    return {
        "endpoint": spec.endpoint,
        "q": spec.q,
        "sort": spec.sort,
        "t": spec.t,
        "limit": spec.limit,
    }


async def enqueue_reddit_task_for_job(session: AsyncSession, job: Job) -> Task:
    """Queue one Reddit fetch task for `job`. Idempotent on `content_hash`.

    Query source priority:

      1. `job.job_plan["reddit_queries"]` if populated (Wave 0 LLM output)
      2. `reddit_queries_for_spec(spec)` — the deterministic template

    All queries land in a single task — `RedditSource.fetch` handles
    partial success internally, so a failed query doesn't poison the
    others' results.
    """
    spec = JobSpec.model_validate(job.spec)
    queries = _queries_from_job_plan(job) or reddit_queries_for_spec(spec)
    params: dict[str, Any] = {"queries": queries}
    content_hash = hash_params({"source": "reddit", "action": "fetch", "params": params})

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
        source="reddit",
        action="fetch",
        params=params,
        content_hash=content_hash,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task
