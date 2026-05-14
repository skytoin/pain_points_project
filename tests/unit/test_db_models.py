"""Tests for `discovery.db.models` — schema shape, defaults, constraints.

Uses an in-memory SQLite (sync engine) so model behavior is testable
without spinning up the async engine. The async engine is tested in
`test_db_engine.py` once it lands.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel

from discovery.db import models
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


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite for each test — full isolation."""
    eng = create_engine("sqlite://", echo=False)
    SQLModel.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_models_module_exports_all_tables() -> None:
    assert hasattr(models, "Job")
    assert hasattr(models, "Task")
    assert hasattr(models, "RawRecordRow")
    assert hasattr(models, "PainSignal")


def test_create_all_registers_four_tables(engine: Engine) -> None:
    expected = {"jobs", "tasks", "raw_records", "pain_signals"}
    assert expected.issubset(SQLModel.metadata.tables.keys())


def test_job_roundtrip_defaults(engine: Engine) -> None:
    """A Job inserts with default status=queued and a timezone-aware created_at."""
    with Session(engine) as session:
        job = Job(spec_hash="a" * 64, spec={"industry": "cleaning"})
        session.add(job)
        session.commit()
        session.refresh(job)

        assert job.id is not None
        assert job.status == JobStatus.queued
        assert job.created_at.tzinfo is not None
        assert job.spec == {"industry": "cleaning"}
        assert job.job_plan is None


def test_task_unique_on_job_id_content_hash(engine: Engine) -> None:
    """`(job_id, content_hash)` is UNIQUE — re-queueing the same fetch fails."""
    with Session(engine) as session:
        job = Job(spec_hash="b" * 64, spec={})
        session.add(job)
        session.commit()
        session.refresh(job)

        first = Task(
            job_id=job.id,
            wave=1,
            source="reddit",
            action="fetch",
            params={"sub": "x"},
            content_hash="c" * 64,
        )
        session.add(first)
        session.commit()

        dup = Task(
            job_id=job.id,
            wave=1,
            source="reddit",
            action="fetch",
            params={"sub": "x"},
            content_hash="c" * 64,
        )
        session.add(dup)
        with pytest.raises(IntegrityError):
            session.commit()


def test_raw_record_unique_on_source_external_id(engine: Engine) -> None:
    """`(source, external_id)` is UNIQUE — second fetch of same Reddit permalink fails."""
    with Session(engine) as session:
        job = Job(spec_hash="d" * 64, spec={})
        session.add(job)
        session.commit()
        session.refresh(job)
        task = Task(
            job_id=job.id,
            wave=1,
            source="reddit",
            action="fetch",
            params={},
            content_hash="e" * 64,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        r1 = RawRecordRow(
            job_id=job.id,
            task_id=task.id,
            source="reddit",
            external_id="t3_abc",
            body={"x": 1},
            content_hash="f" * 64,
        )
        session.add(r1)
        session.commit()

        r2 = RawRecordRow(
            job_id=job.id,
            task_id=task.id,
            source="reddit",
            external_id="t3_abc",
            body={"x": 1},
            content_hash="9" * 64,
        )
        session.add(r2)
        with pytest.raises(IntegrityError):
            session.commit()


# Note on validation: SQLModel `table=True` classes intentionally skip
# Pydantic __init__ validation so SQLAlchemy can map raw rows back into
# instances cheaply. The `ge=1, le=5` and `ge=0, le=1` constraints on
# PainSignal stay on the model as documentation, but they don't fire on
# construction. Real validation happens upstream at the LLM-station
# boundary — `discovery.llm.schemas.PainExtraction` is the non-table
# Pydantic schema that the LLM is forced to produce. That class lands
# with Wave 2 and gets its own validation tests there.


def test_pain_signal_transitional_json_columns_roundtrip(engine: Engine) -> None:
    """The transitional `tools_mentioned` / `company_mentions` JSON columns
    round-trip as `list[str]`. These columns are marked transitional in the
    model — a future migration backfills dedicated tables and drops them.
    """
    with Session(engine) as session:
        job = Job(spec_hash="h" * 64, spec={})
        session.add(job)
        session.commit()
        session.refresh(job)
        task = Task(
            job_id=job.id,
            wave=2,
            source="llm:pain_extraction",
            action="classify",
            params={},
            content_hash="i" * 64,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        rec = RawRecordRow(
            job_id=job.id,
            task_id=task.id,
            source="reddit",
            external_id="t3_xyz",
            body={},
            content_hash="j" * 64,
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)

        signal = PainSignal(
            job_id=job.id,
            raw_record_id=rec.id,
            pain_topic=PainTopic.billing,
            sentiment=Sentiment.negative,
            severity=4,
            industry_signal_strength=0.8,
            quote="Stripe fees are killing us",
            tools_mentioned=["Stripe", "Paypal"],
            company_mentions=["Acme Corp"],
        )
        session.add(signal)
        session.commit()
        session.refresh(signal)

        assert signal.id is not None
        assert signal.tools_mentioned == ["Stripe", "Paypal"]
        assert signal.company_mentions == ["Acme Corp"]


def test_task_status_default_is_queued(engine: Engine) -> None:
    """A new Task starts queued — the worker flips it to running on claim."""
    with Session(engine) as session:
        job = Job(spec_hash="k" * 64, spec={})
        session.add(job)
        session.commit()
        session.refresh(job)

        task = Task(
            job_id=job.id,
            wave=1,
            source="reddit",
            action="fetch",
            params={},
            content_hash="l" * 64,
        )
        session.add(task)
        session.commit()
        session.refresh(task)

        assert task.status == TaskStatus.queued
        assert task.attempts == 0
        assert task.max_attempts == 3
        assert task.claimed_at is None
        assert task.completed_at is None
