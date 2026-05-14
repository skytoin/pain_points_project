"""Async engine + session factory for the discovery database.

Two surfaces:

- `create_engine_for(url)` — explicit factory. Tests call this with an
  in-memory URL; production callers don't.
- `get_engine()` — cached singleton bound to `settings.database_url`.
  This is what the orchestrator and workers use.

Sessions are produced by `async_session_factory(engine)`, which returns
a `sessionmaker` that yields `sqlmodel.ext.asyncio.session.AsyncSession`
(the SQLModel async session that knows about model classes). Callers
use it via `async with maker() as session: ...`.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


def create_engine_for(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an async engine for the given URL.

    Parameters
    ----------
    url :
        A SQLAlchemy async URL, e.g. `sqlite+aiosqlite:///data/discovery.db`
        or `sqlite+aiosqlite:///:memory:` for tests.
    echo :
        Log all SQL to stderr. Off by default.
    """
    return create_async_engine(url, echo=echo, future=True)


def async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return a session maker bound to `engine`.

    `expire_on_commit=False` keeps attributes accessible after commit —
    we read commonly after committing in workers.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Return the cached production engine bound to `settings.database_url`.

    The settings import is deliberately lazy: tests that exercise
    `create_engine_for(...)` directly never load settings (which would
    require `ANTHROPIC_API_KEY` to be present in the environment).
    """
    from discovery.config.settings import settings  # noqa: PLC0415 — lazy on purpose

    return create_engine_for(settings.database_url)
