"""SQLModel table definitions for the discovery pipeline.

This file owns the Bronze layer (`raw_records`), the control plane
(`jobs`, `tasks`), and the first-pass Silver layer (`pain_signals`).
Later waves add `companies`, `tools`, `reviews`, `job_postings`,
`tools_mentioned`, etc. — those tables don't exist yet and will be
added via separate alembic migrations when their wave lands.

Conventions
-----------
- StrEnums are stored as `VARCHAR` with *no* SQL `CHECK` constraint
  (`native_enum=False`, `create_constraint=False`). Python-side
  validation is enough; CHECK constraints fight you on every enum
  extension.
- All datetimes are timezone-aware UTC (`DateTime(timezone=True)`).
- `content_hash` columns are 64-char sha256 hex digests produced by
  `discovery.hashing.hash_params`.
- The Pydantic DTO `RawRecord` lives in `discovery.sources.base`; the
  database row is `RawRecordRow` here. Different layers, different
  names — keeps imports unambiguous.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, DateTime, Index
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator
from sqlmodel import JSON, Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Always-aware UTC datetimes, even on SQLite.

    SQLite has no native `TIMESTAMP WITH TIME ZONE` type — values written
    by SQLAlchemy come back without `tzinfo`. This wrapper:

    - On bind: refuse naive datetimes (callers must be explicit); convert
      aware values to UTC and strip tzinfo before storage.
    - On read: re-attach `UTC` tzinfo.

    Postgres has native `timestamptz` and will handle this transparently;
    the decorator is still safe to use.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime cannot be stored — pass an aware UTC datetime.")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def _enum_column[E: StrEnum](
    enum_cls: type[E], *, default: str | None = None, nullable: bool = False
) -> Column[E]:
    """Build a `VARCHAR` column for a StrEnum, without a SQL CHECK constraint."""
    return Column(
        SAEnum(enum_cls, native_enum=False, create_constraint=False, length=32),
        nullable=nullable,
        default=default,
    )


def _utc_column(*, nullable: bool = False) -> Column[datetime]:
    return Column(UtcDateTime(), nullable=nullable)


# --- Enums -------------------------------------------------------------------


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class TaskStatus(StrEnum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class PainTopic(StrEnum):
    scheduling = "scheduling"
    billing = "billing"
    follow_up = "follow_up"
    lead_response = "lead_response"
    staffing = "staffing"
    tools = "tools"
    other = "other"


class Sentiment(StrEnum):
    negative = "negative"
    neutral = "neutral"
    positive = "positive"


# --- Tables ------------------------------------------------------------------


class Job(SQLModel, table=True):
    """One discovery run (Wave 0 → Wave 5)."""

    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    spec_hash: str = Field(index=True, unique=True, max_length=64)
    spec: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    job_plan: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    status: JobStatus = Field(
        default=JobStatus.queued,
        sa_column=_enum_column(JobStatus, default=JobStatus.queued.value),
    )
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_utc_column())
    started_at: datetime | None = Field(default=None, sa_column=_utc_column(nullable=True))
    completed_at: datetime | None = Field(default=None, sa_column=_utc_column(nullable=True))
    last_error: str | None = None


class Task(SQLModel, table=True):
    """One unit of work for the worker pool.

    LLM calls are tasks too. The `source` column distinguishes them with
    an `llm:` prefix (e.g. `llm:pain_extraction`). See architecture.md
    ("LLM calls are tasks, not function calls").
    """

    __tablename__ = "tasks"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    wave: int = Field(index=True)
    source: str = Field(max_length=64)
    action: str = Field(max_length=64)
    params: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: TaskStatus = Field(
        default=TaskStatus.queued,
        sa_column=_enum_column(TaskStatus, default=TaskStatus.queued.value),
    )
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)
    last_error: str | None = None
    content_hash: str = Field(max_length=64)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_utc_column())
    claimed_at: datetime | None = Field(default=None, sa_column=_utc_column(nullable=True))
    completed_at: datetime | None = Field(default=None, sa_column=_utc_column(nullable=True))

    __table_args__ = (
        # Worker claim query: WHERE status='queued' ORDER BY wave, created_at LIMIT 1.
        Index("ix_tasks_claim", "status", "wave", "claimed_at"),
        # Stuck-task sweep: WHERE status='running' AND claimed_at < now-10min.
        Index("ix_tasks_stuck", "status", "claimed_at"),
        # Idempotency: same (job, action+params hash) cannot be queued twice.
        Index("ix_tasks_idem", "job_id", "content_hash", unique=True),
    )


class RawRecordRow(SQLModel, table=True):
    """One Bronze-layer row — verbatim API response, untouched.

    Distinct from `discovery.sources.base.RawRecord` (the Pydantic DTO
    returned by source adapters). The worker turns DTOs into rows on
    insert; the DTO knows nothing about job_id / task_id / db state.
    """

    __tablename__ = "raw_records"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    source: str = Field(max_length=64)
    external_id: str = Field(max_length=256)
    fetched_at: datetime = Field(default_factory=_utcnow, sa_column=_utc_column())
    body: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    content_hash: str = Field(max_length=64)

    __table_args__ = (
        # Natural dedup: same source seeing the same record twice is a no-op.
        Index("ix_raw_natural", "source", "external_id", unique=True),
        # Common query: per-job per-source counts.
        Index("ix_raw_job_source", "job_id", "source"),
    )


class PainSignal(SQLModel, table=True):
    """One Silver-layer pain signal — output of Wave 2 LLM classification."""

    __tablename__ = "pain_signals"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    raw_record_id: int = Field(foreign_key="raw_records.id", index=True)
    pain_topic: PainTopic = Field(sa_column=_enum_column(PainTopic))
    sentiment: Sentiment = Field(sa_column=_enum_column(Sentiment))
    severity: int = Field(ge=1, le=5)
    industry_signal_strength: float = Field(ge=0, le=1)
    quote: str

    # transitional — moves to a dedicated `tools_mentioned` table when the
    # tools wave lands. Keeping JSON now so we don't lose extracted data.
    tools_mentioned: list[str] = Field(
        sa_column=Column(JSON, nullable=False),
        default_factory=list,
    )
    # transitional — moves to `signal_company_links` when Wave 5 lands.
    company_mentions: list[str] = Field(
        sa_column=Column(JSON, nullable=False),
        default_factory=list,
    )
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_utc_column())
