"""Tests for `discovery.jobs` — JobSpec validation and idempotent creation.

Pins three contracts:

1. A spec without `as_of` is rejected at validation time. Without that
   anchor, re-running monthly would collide on `spec_hash` and refuse
   to create a new job.
2. The same spec inserted twice returns the same row (no duplicates).
3. A different `as_of` produces a different `spec_hash` → a new job —
   this is the "run again next month" use case.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date

import pytest
from pydantic import ValidationError
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 — registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.hashing import hash_params
from discovery.jobs import JobSpec, create_job


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()


# --- JobSpec validation -----------------------------------------------------


class TestJobSpec:
    def test_requires_as_of(self) -> None:
        """No `as_of` → validation error. This is the core date-anchor rule."""
        with pytest.raises(ValidationError):
            JobSpec(industry="commercial cleaning")  # type: ignore[call-arg]

    def test_requires_industry(self) -> None:
        with pytest.raises(ValidationError):
            JobSpec(as_of=date(2026, 6, 1))  # type: ignore[call-arg]

    def test_rejects_empty_industry(self) -> None:
        with pytest.raises(ValidationError):
            JobSpec(industry="", as_of=date(2026, 6, 1))

    def test_accepts_iso_string_for_as_of(self) -> None:
        """Pydantic coerces ISO strings to `date` — the API can send JSON."""
        spec = JobSpec(industry="x", as_of="2026-06-01")  # type: ignore[arg-type]
        assert spec.as_of == date(2026, 6, 1)

    def test_tolerates_extra_keys(self) -> None:
        """Forward-compat: Wave 0 may add fields (NAICS codes etc.). Spec
        keeps them, and they participate in the hash."""
        spec = JobSpec(
            industry="x",
            as_of=date(2026, 6, 1),
            naics="561720",  # type: ignore[call-arg]
        )
        dumped = spec.model_dump(mode="json")
        assert dumped["naics"] == "561720"

    def test_dump_serializes_as_of_as_iso_string(self) -> None:
        """`mode="json"` ensures the date round-trips through JSON
        cleanly — important because `Job.spec` is a JSON column."""
        spec = JobSpec(industry="x", as_of=date(2026, 6, 1))
        dumped = spec.model_dump(mode="json")
        assert dumped["as_of"] == "2026-06-01"
        assert isinstance(dumped["as_of"], str)


class TestJobSpecTimeWindow:
    """Reddit's `t` parameter values, exposed at the spec level so users
    can widen the search window for niche topics (skill item 11).
    """

    def test_defaults_to_month(self) -> None:
        spec = JobSpec(industry="x", as_of=date(2026, 6, 1))
        assert spec.time_window == "month"

    def test_accepts_all_reddit_values(self) -> None:
        for value in ("hour", "day", "week", "month", "year", "all"):
            spec = JobSpec(industry="x", as_of=date(2026, 6, 1), time_window=value)
            assert spec.time_window == value

    def test_rejects_invalid_value(self) -> None:
        with pytest.raises(ValidationError):
            JobSpec(
                industry="x",
                as_of=date(2026, 6, 1),
                time_window="decade",  # type: ignore[arg-type]
            )

    def test_time_window_participates_in_hash(self) -> None:
        """Two specs differing only by time_window must produce different
        spec_hashes so monthly vs yearly runs don't collide in the cache."""
        a = JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="month")
        b = JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year")
        assert hash_params(a.model_dump(mode="json")) != hash_params(b.model_dump(mode="json"))


# --- create_job -------------------------------------------------------------


class TestCreateJob:
    async def test_creates_new_job_for_unseen_spec(self, session: AsyncSession) -> None:
        spec = JobSpec(
            industry="commercial cleaning",
            as_of=date(2026, 6, 1),
            location="NY",
            size="medium",
        )
        job = await create_job(session, spec)

        assert job.id is not None
        assert len(job.spec_hash) == 64
        assert job.spec["industry"] == "commercial cleaning"
        assert job.spec["as_of"] == "2026-06-01"
        assert job.spec["location"] == "NY"

    async def test_returns_existing_job_for_identical_spec(self, session: AsyncSession) -> None:
        """Idempotency: same spec twice → same row, never a duplicate."""
        spec = JobSpec(industry="x", as_of=date(2026, 6, 1))
        first = await create_job(session, spec)
        second = await create_job(session, spec)

        assert first.id == second.id
        assert first.spec_hash == second.spec_hash

    async def test_different_as_of_yields_different_job(self, session: AsyncSession) -> None:
        """The whole point: monthly re-runs each get their own job."""
        may = await create_job(
            session,
            JobSpec(industry="cleaning", as_of=date(2026, 5, 1), location="NY"),
        )
        june = await create_job(
            session,
            JobSpec(industry="cleaning", as_of=date(2026, 6, 1), location="NY"),
        )

        assert may.id != june.id
        assert may.spec_hash != june.spec_hash

    async def test_accepts_plain_dict_and_validates_it(self, session: AsyncSession) -> None:
        """Callers can pass a dict instead of a JobSpec; the factory validates."""
        job = await create_job(
            session,
            {"industry": "cleaning", "as_of": "2026-06-01"},
        )
        assert job.spec["as_of"] == "2026-06-01"

    async def test_rejects_dict_missing_as_of(self, session: AsyncSession) -> None:
        """Dict input goes through the same validation — no escape hatch."""
        with pytest.raises(ValidationError):
            await create_job(session, {"industry": "cleaning"})

    async def test_spec_hash_is_key_order_invariant(self, session: AsyncSession) -> None:
        """Reordering keys in the dict input doesn't change the hash."""
        a = await create_job(
            session,
            {"industry": "x", "as_of": "2026-06-01", "location": "NY"},
        )
        b = await create_job(
            session,
            {"location": "NY", "as_of": "2026-06-01", "industry": "x"},
        )
        assert a.id == b.id  # deduped via hash
