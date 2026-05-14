"""Wave 1 orchestration for Reddit — hand-rolled query template.

Until Wave 0's LLM query expansion lands, queries are generated from a
fixed template: baseline business/startup subreddits crossed with a few
pain-phrase categories from the `reddit-source` skill. The result is a
small set of site-wide OR-compressed queries that scan baseline subs
for `<industry>` plus pain signals.

When Wave 0 ships, this module becomes the fallback path for when the
LLM call fails or returns invalid output — per the architecture doc:
"a deterministic fallback uses a hand-written mapping table for the
top 10 industries".

Public surface
--------------
- `reddit_queries_for_spec(spec)` — pure helper that returns a list of
  query dicts accepted by `RedditSource.fetch`.
- `enqueue_reddit_task_for_job(session, job)` — idempotent: inserts at
  most one Reddit task per job (UNIQUE on `(job_id, content_hash)`).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job, Task
from discovery.hashing import hash_params
from discovery.jobs import JobSpec

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
    """Build the Wave 1 Reddit query plan for `spec`.

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


async def enqueue_reddit_task_for_job(session: AsyncSession, job: Job) -> Task:
    """Queue one Reddit fetch task for `job`. Idempotent on `content_hash`.

    All baseline queries go into a single task — `RedditSource.fetch`
    handles partial success internally, so a failed query doesn't poison
    the others' results. If you wanted per-query retry granularity, you'd
    split into one task per query instead.
    """
    spec = JobSpec.model_validate(job.spec)
    queries = reddit_queries_for_spec(spec)
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
