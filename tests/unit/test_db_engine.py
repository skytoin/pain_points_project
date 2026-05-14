"""Tests for `discovery.db.engine` — async engine factory + session maker.

The async engine is what the orchestrator and workers use to talk to
SQLite (and later Postgres). The factory is parameterized on URL so the
production code can read from settings while tests pass an in-memory
URL explicitly.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from discovery.db import models  # noqa: F401 — registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, JobStatus


async def test_create_async_engine_with_explicit_url() -> None:
    """The factory accepts an explicit URL and returns a usable async engine."""
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


async def test_async_engine_can_create_all_tables() -> None:
    """The engine + the models metadata interoperate.

    `SQLModel.metadata.create_all` is sync — we run it via
    `engine.begin()` + `conn.run_sync(...)` per SQLAlchemy's async DDL
    pattern.
    """
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
    finally:
        await engine.dispose()


async def test_async_session_factory_yields_session() -> None:
    """`async_session_factory(engine)` returns an `AsyncSession`-yielding maker."""
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    maker = async_session_factory(engine)
    try:
        async with maker() as session:
            assert isinstance(session, AsyncSession)
    finally:
        await engine.dispose()


async def test_engine_supports_basic_roundtrip() -> None:
    """End-to-end: build engine, create tables, insert a Job, read it back."""
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        async with SQLModelAsyncSession(engine) as session:
            session.add(Job(spec_hash="x" * 64, spec={"k": "v"}))
            await session.commit()

            result = await session.exec(select(Job))
            jobs = list(result.all())
            assert len(jobs) == 1
            assert jobs[0].status == JobStatus.queued
            assert jobs[0].spec == {"k": "v"}
            assert jobs[0].created_at.tzinfo is not None
    finally:
        await engine.dispose()
