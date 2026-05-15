"""Job-level concerns — spec validation and idempotent job creation.

A `Job` is one complete discovery run anchored to a date. The date
anchor (`as_of`) is **required** — without it, re-running the same
industry next month would collide on `spec_hash` and refuse to create
a new job. With it, monthly re-runs each get their own row, and the
Bronze-layer dedup (`(source, external_id)` UNIQUE) means popular
posts seen by multiple runs are stored once.

Public surface
--------------
- `JobSpec` — Pydantic schema for the user's discovery spec. Requires
  `industry` and `as_of`; tolerates extra keys so Wave 0 can add
  fields (NAICS codes, employee ranges, etc.) without breaking the
  contract.
- `create_job(session, spec)` — validate + hash + insert-or-return.
  Idempotent on `spec_hash`.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job
from discovery.hashing import hash_params


class JobSpec(BaseModel):
    """Validated input for a discovery run.

    `as_of` is required. It's the only thing distinguishing this month's
    run from last month's — without it the hash collides and we'd
    refuse to insert a second job.

    Extra keys are allowed and round-trip through the hash, so future
    spec additions (e.g. `naics_codes`, `employee_range`) don't break
    older code; they just produce a different hash, which is correct
    behavior.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    industry: str = Field(min_length=1)
    as_of: date
    location: str | None = None
    size: str | None = None
    time_window: Literal["hour", "day", "week", "month", "year", "all"] = Field(
        default="month",
        description=(
            "Reddit's `t` parameter — how far back the LLM-built queries "
            "should search. Default `month` is fine for active topics; "
            "use `year` for niche / B2B topics where a month doesn't "
            "produce enough signal (skill item 11)."
        ),
    )


async def create_job(session: AsyncSession, spec: JobSpec | dict[str, Any]) -> Job:
    """Create a `Job` for `spec`, or return the existing one with the same hash.

    Idempotent: calling this twice with the same spec returns the same
    `Job` row. Calling it with a different `as_of` (or any other spec
    field) produces a new row.

    Plain dict input is accepted and validated through `JobSpec`.
    """
    spec_model = spec if isinstance(spec, JobSpec) else JobSpec.model_validate(spec)
    spec_dict = spec_model.model_dump(mode="json")
    spec_hash = hash_params(spec_dict)

    existing = await session.exec(select(Job).where(Job.spec_hash == spec_hash))
    job = existing.first()
    if job is not None:
        return job

    job = Job(spec_hash=spec_hash, spec=spec_dict)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job
