"""Tests for `discovery.config.settings`.

Settings are read from env vars. Tests use `monkeypatch.setenv` and
construct a Settings instance with `_env_file=None` so the real `.env`
file isn't read — keeps tests hermetic and reproducible regardless of
what the developer has locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from discovery.config.settings import Settings, _find_project_root


class TestSettings:
    def test_openai_api_key_is_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent OPENAI_API_KEY → field is None; existing Anthropic-only flows keep booting."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.openai_api_key is None

    def test_openai_api_key_loads_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OPENAI_API_KEY env var populates the field as a SecretStr."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert isinstance(s.openai_api_key, SecretStr)
        assert s.openai_api_key.get_secret_value() == "sk-openai-test"


class TestDatabaseUrl:
    """`database_url` reads only from `DISCOVERY_DATABASE_URL`, not the
    bare `DATABASE_URL`. The main project sharing this `.env` has a
    different `DATABASE_URL` (Postgres) that would otherwise pollute
    our SQLite default.
    """

    def test_bare_database_url_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("DATABASE_URL", "postgresql://someone:secret@host/db")
        monkeypatch.delenv("DISCOVERY_DATABASE_URL", raising=False)
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        # Default kicks in; the Postgres URL is ignored.
        assert s.database_url.startswith("sqlite")

    def test_discovery_database_url_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("DISCOVERY_DATABASE_URL", "sqlite+aiosqlite:///custom.db")
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.database_url == "sqlite+aiosqlite:///custom.db"


class TestFindProjectRoot:
    """`_find_project_root` walks up to the main project when running
    inside a worktree, so every worktree shares one `.env` file.
    """

    def test_main_project_layout_returns_parents_3(self) -> None:
        """Standard checkout: settings.py at <root>/src/discovery/config/settings.py."""
        here = Path("/home/me/my-project/src/discovery/config/settings.py")
        assert _find_project_root(here) == Path("/home/me/my-project")

    def test_worktree_layout_returns_main_project(self) -> None:
        """Worktree at <root>/.claude/worktrees/<name>/src/...: walks up to <root>."""
        here = Path(
            "/home/me/my-project/.claude/worktrees/feature-x/src/discovery/config/settings.py"
        )
        assert _find_project_root(here) == Path("/home/me/my-project")

    def test_windows_style_worktree_path(self) -> None:
        """Same logic, Windows-style separators."""
        here = Path(
            r"C:\Users\skyto\pain_points_poject\.claude\worktrees\quirky-mcclintock-17ee22"
            r"\src\discovery\config\settings.py"
        )
        assert _find_project_root(here) == Path(r"C:\Users\skyto\pain_points_poject")

    def test_dot_claude_without_worktrees_does_not_climb(self) -> None:
        """A `.claude` directory that isn't followed by `worktrees` stays put.
        (Some projects put plain `.claude/` configs at the project root.)
        """
        here = Path("/home/me/.claude-stuff/proj/src/discovery/config/settings.py")
        assert _find_project_root(here) == Path("/home/me/.claude-stuff/proj")
