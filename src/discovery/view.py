"""Read-only views over the DB for the inspection CLI.

Pure data layer — no Rich rendering, no logging. Functions take a session,
return Pydantic models. The CLI in `discovery.cli.inspect` formats these.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job, RawRecordRow
from discovery.llm.schemas import JobPlan


class JobSummary(BaseModel):
    """One-line view of a Job. Used by `discovery jobs`."""

    model_config = ConfigDict(frozen=True)

    id: int
    industry: str
    as_of: str
    status: str
    planned: bool
    post_count: int
    created_at: datetime


class PostView(BaseModel):
    """One Reddit post extracted from `RawRecordRow.body`."""

    model_config = ConfigDict(frozen=True)

    external_id: str
    subreddit: str
    title: str
    score: int
    num_comments: int
    permalink: str
    body_preview: str


class JobDetail(BaseModel):
    """Full view of a Job. Used by `discovery show <id>`."""

    model_config = ConfigDict(frozen=True)

    summary: JobSummary
    plan: JobPlan | None
    posts: list[PostView]


async def gather_job_summaries(session: AsyncSession) -> list[JobSummary]:
    """Return one `JobSummary` per Job in the DB, newest first."""
    jobs = list(
        (await session.exec(select(Job).order_by(Job.created_at.desc()))).all()  # type: ignore[attr-defined]
    )
    out: list[JobSummary] = []
    for job in jobs:
        if job.id is None:
            continue
        count = await _count_posts(session, job.id)
        out.append(_make_summary(job, count))
    return out


async def gather_job_detail(
    session: AsyncSession, *, job_id: int, post_limit: int = 10
) -> JobDetail | None:
    """Return a `JobDetail` for `job_id`, or None if no such Job."""
    job = await session.get(Job, job_id)
    if job is None or job.id is None:
        return None

    count = await _count_posts(session, job.id)
    plan = _safe_load_plan(job.job_plan)

    post_rows = list(
        (
            await session.exec(
                select(RawRecordRow)
                .where(RawRecordRow.job_id == job.id)
                .order_by(RawRecordRow.fetched_at.desc())  # type: ignore[attr-defined]
                .limit(post_limit)
            )
        ).all()
    )
    posts = [_extract_post(r) for r in post_rows]

    return JobDetail(
        summary=_make_summary(job, count),
        plan=plan,
        posts=posts,
    )


async def _count_posts(session: AsyncSession, job_id: int) -> int:
    """Return the number of raw_records rows for `job_id`."""
    row = await session.exec(
        select(func.count()).select_from(RawRecordRow).where(RawRecordRow.job_id == job_id)
    )
    return row.first() or 0


def _make_summary(job: Job, post_count: int) -> JobSummary:
    spec: dict[str, Any] = job.spec or {}
    return JobSummary(
        id=job.id or 0,
        industry=str(spec.get("industry", "?")),
        as_of=str(spec.get("as_of", "?")),
        status=job.status.value,
        planned=job.job_plan is not None,
        post_count=post_count,
        created_at=job.created_at,
    )


def _safe_load_plan(raw: dict[str, Any] | None) -> JobPlan | None:
    """Validate a stored job_plan dict; return None if it's null or stale."""
    if raw is None:
        return None
    try:
        return JobPlan.model_validate(raw)
    except Exception:
        return None


def _extract_post(row: RawRecordRow) -> PostView:
    """Pull the human-readable fields out of a Reddit raw_records row.

    The Reddit adapter stores the inner post `data` dict as `body`
    directly (the outer `{"kind": "t3", "data": ...}` envelope is
    stripped on the way in). So all the readable fields live at the
    top level of `body`.
    """
    body = row.body if isinstance(row.body, dict) else {}
    preview = (body.get("selftext") or "").strip().replace("\n", " ")
    if len(preview) > 200:
        preview = preview[:200] + "..."
    return PostView(
        external_id=row.external_id,
        subreddit=str(body.get("subreddit", "?")),
        title=str(body.get("title", "(no title)")),
        score=int(body.get("score", 0) or 0),
        num_comments=int(body.get("num_comments", 0) or 0),
        permalink=str(body.get("permalink", "")),
        body_preview=preview,
    )
