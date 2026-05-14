"""Alembic environment.

Reads the database URL from `discovery.config.settings.settings` (so we
have one source of truth for the connection string) and uses
`SQLModel.metadata` as the target for autogenerate.

SQLite-specific: `render_as_batch=True` enables alembic's batch-mode
ALTER emulation so future migrations that add/drop/alter columns work.
SQLite has very limited native ALTER TABLE support; batch mode copies
the table under the hood. No effect on Postgres.

Note on async URL: production code uses `sqlite+aiosqlite://...` for the
async runtime. Alembic runs synchronously, so we swap the driver to the
plain `sqlite://` form when handing the URL to the engine factory.
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel
from sqlmodel.sql.sqltypes import AutoString

from discovery.config.settings import settings

# Side-effect import: registers every table on SQLModel.metadata.
from discovery.db import models  # noqa: F401
from discovery.db.models import UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _sync_url(async_url: str) -> str:
    """Translate the production async URL to its sync equivalent."""
    return async_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg2")


# Override the URL from alembic.ini with the one from settings.
config.set_main_option("sqlalchemy.url", _sync_url(settings.database_url))

target_metadata = SQLModel.metadata


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Keep generated migrations free of app-code imports.

    `UtcDateTime` is our TypeDecorator wrapping `DateTime(timezone=True)`;
    for DDL purposes the wrapper is invisible, so we emit the plain form.

    `AutoString` is SQLModel's String wrapper; for DDL it's just String.
    Rendering it as `sa.String` removes the `import sqlmodel` dependency
    from every migration file.
    """
    if type_ == "type":
        if isinstance(obj, UtcDateTime):
            return "sa.DateTime(timezone=True)"
        if isinstance(obj, AutoString):
            length = obj.length
            return f"sa.String(length={length})" if length else "sa.String()"
    return False


def run_migrations_offline() -> None:
    """Run migrations without a DBAPI — emits SQL to stdout."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database via a synchronous engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
