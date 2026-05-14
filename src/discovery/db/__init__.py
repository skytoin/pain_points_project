"""Database layer — SQLModel tables, async engine, session factory.

Public surface:

- Tables: `Job`, `Task`, `RawRecordRow`, `PainSignal`
- Enums: `JobStatus`, `TaskStatus`, `PainTopic`, `Sentiment`
- Engine: `create_engine_for(url)`, `get_engine()`,
  `async_session_factory(engine)`

The Pydantic DTO `RawRecord` (returned by source adapters) lives in
`discovery.sources.base`, not here — different layer, different concern.
"""

from __future__ import annotations

from discovery.db.engine import (
    async_session_factory,
    create_engine_for,
    get_engine,
)
from discovery.db.models import (
    Job,
    JobStatus,
    PainSignal,
    PainTopic,
    RawRecordRow,
    Sentiment,
    Task,
    TaskStatus,
)

__all__ = [
    "Job",
    "JobStatus",
    "PainSignal",
    "PainTopic",
    "RawRecordRow",
    "Sentiment",
    "Task",
    "TaskStatus",
    "async_session_factory",
    "create_engine_for",
    "get_engine",
]
