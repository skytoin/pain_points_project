"""Typed configuration for the whole project.

We use `pydantic-settings`, which reads values from environment variables
(and from a `.env` file in development). Every setting is type-checked at
startup, so a typo or a missing key fails loudly instead of mysteriously
hours later.

Usage:

    from discovery.config.settings import settings

    api_key = settings.anthropic_api_key
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root(here: Path) -> Path:
    """Return the project root for a settings.py at `here`.

    Standard checkout: `<root>/src/discovery/config/settings.py` → `<root>`
    (parents[3]).

    Worktree checkout: `<root>/.claude/worktrees/<name>/src/discovery/config/settings.py`
    → `<root>`. This means every worktree shares the main project's `.env`
    file, instead of each worktree needing its own copy of secrets.
    """
    standard = here.parents[3]
    parts = standard.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return Path(*parts[:i])
    return standard


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())


class Settings(BaseSettings):
    """All runtime configuration.

    Field names match environment variable names, lower-cased.
    Example: `ANTHROPIC_API_KEY` in `.env` becomes
    `settings.anthropic_api_key` in Python.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM -------------------------------------------------------
    anthropic_api_key: SecretStr
    openai_api_key: SecretStr | None = None

    # ---- Source APIs (all optional — None if not set) -------------
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    reddit_user_agent: str = "discovery-pipeline/0.1"

    google_api_key: SecretStr | None = None
    yelp_api_key: SecretStr | None = None
    apollo_api_key: SecretStr | None = None
    apify_token: SecretStr | None = None
    hunter_api_key: SecretStr | None = None
    newsapi_key: SecretStr | None = None
    listen_notes_api_key: SecretStr | None = None
    opencorporates_api_key: SecretStr | None = None
    product_hunt_token: SecretStr | None = None
    theirstack_api_key: SecretStr | None = None

    adzuna_app_id: SecretStr | None = None
    adzuna_app_key: SecretStr | None = None

    # ---- Runtime ---------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///data/discovery.db",
        description="SQLAlchemy URL. Defaults to a local SQLite file.",
        # Bound to DISCOVERY_DATABASE_URL only, not bare DATABASE_URL.
        # The main project sharing this `.env` defines DATABASE_URL for
        # its own (different) database; we don't want that bleeding in.
        validation_alias="DISCOVERY_DATABASE_URL",
    )
    llm_cache_dir: Path = Field(default=PROJECT_ROOT / ".diskcache" / "llm")
    log_level: str = "INFO"

    sentry_dsn: SecretStr | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the (cached) Settings singleton.

    Cached because parsing env vars + validating types is non-zero work
    and the values don't change at runtime.
    """
    return Settings()  # type: ignore[call-arg]


# Convenience: most callers just do `from ... import settings`.
settings = get_settings()
