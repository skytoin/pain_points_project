"""Cross-source job-level orchestration.

`plan_job(session, job)` runs Wave 0 (LLM query expansion) inline and
populates `Job.job_plan`. On failure it leaves `job_plan` null and
returns the unchanged Job - the per-source orchestrators detect a null
`job_plan` and fall back to their deterministic templates.

NOTE TO FUTURE SESSIONS
-----------------------
This is the "inline Option A" implementation. The task-based "Option B"
alternative (running Wave 0 as a worker task) was considered and
deferred. See `docs/plans/2026-05-14-wave-0-query-expansion.md` for
the decision record and the ~20-line promotion path if/when you
decide to make the switch.
"""

from __future__ import annotations

from loguru import logger
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job
from discovery.jobs import JobSpec
from discovery.llm.stations.query_expansion import (
    QueryExpansionError,
    run_query_expansion,
)


async def plan_job(session: AsyncSession, job: Job) -> Job:
    """Run Wave 0 query expansion for `job`. Idempotent and fault-tolerant.

    - If `job.job_plan` is already populated, returns the job unchanged
      (re-running `discovery run` shouldn't burn a second LLM call).
    - On success: writes the JobPlan dict to `job.job_plan`, commits.
    - On `QueryExpansionError`: logs a warning, leaves `job.job_plan`
      null, returns the job unchanged. Per-source orchestrators must
      detect a null `job_plan` and fall back to their templates.

    Returns the (possibly updated) Job.
    """
    if job.job_plan is not None:
        logger.debug("plan_job: job {} already planned; skipping LLM call", job.id)
        return job

    spec = JobSpec.model_validate(job.spec)
    try:
        plan = await run_query_expansion(spec)
    except QueryExpansionError as e:
        logger.warning(
            "plan_job: query expansion failed for job {}: {}; "
            "Reddit orchestrator will use the deterministic template.",
            job.id,
            e,
        )
        return job

    job.job_plan = plan.model_dump()
    session.add(job)
    await session.commit()
    await session.refresh(job)
    logger.info(
        "plan_job: job {} planned with {} reddit_queries",
        job.id,
        len(plan.reddit_queries),
    )
    return job
