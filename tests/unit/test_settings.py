"""Tests for `discovery.config.settings`.

Settings are read from env vars. Tests use `monkeypatch.setenv` and
construct a Settings instance with `_env_file=None` so the real `.env`
file isn't read — keeps tests hermetic and reproducible regardless of
what the developer has locally.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from discovery.config.settings import Settings


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
