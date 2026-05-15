"""Tests for `discovery.llm.client.call_openai`.

Monkeypatch the lazy client getter so no real network is hit. Verify
the OpenAI-specific quirks: `system` is folded into the messages array
as a `developer` role, and lazy initialization respects a None API key
(so the station can fall back cleanly).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from discovery.llm import client as client_module
from discovery.llm.client import call_openai


class _Echo(BaseModel):
    msg: str


class _FakeOpenAICompletions:
    def __init__(self) -> None:
        self.last_call: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> _Echo:
        self.last_call = kwargs
        return _Echo(msg="ok")


class _FakeOpenAIChat:
    def __init__(self) -> None:
        self.completions = _FakeOpenAICompletions()


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.chat = _FakeOpenAIChat()


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> _FakeOpenAIClient:
    fake = _FakeOpenAIClient()
    monkeypatch.setattr(client_module, "_get_openai_client", lambda: fake)
    return fake


class TestCallOpenAI:
    async def test_returns_validated_pydantic(self, fake_openai: _FakeOpenAIClient) -> None:
        result = await call_openai(
            system="sys",
            user="usr",
            response_model=_Echo,
            model="gpt-5.4",
        )
        assert isinstance(result, _Echo)
        assert result.msg == "ok"

    async def test_folds_system_into_messages_as_developer_role(
        self, fake_openai: _FakeOpenAIClient
    ) -> None:
        """gpt-5.x renamed `system` → `developer`. We use the modern spelling."""
        await call_openai(
            system="you are an analyst",
            user="hi",
            response_model=_Echo,
            model="gpt-5.4",
        )
        call = fake_openai.chat.completions.last_call
        assert call is not None
        assert call["messages"] == [
            {"role": "developer", "content": "you are an analyst"},
            {"role": "user", "content": "hi"},
        ]

    async def test_passes_model_and_temperature(self, fake_openai: _FakeOpenAIClient) -> None:
        await call_openai(
            system="s",
            user="u",
            response_model=_Echo,
            model="gpt-5.4",
            temperature=0.2,
        )
        call = fake_openai.chat.completions.last_call
        assert call is not None
        assert call["model"] == "gpt-5.4"
        assert call["temperature"] == 0.2


class TestLazyClient:
    def test_raises_when_api_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If openai_api_key is None, building the client raises a clear
        error so the station can fall back."""
        monkeypatch.setattr(client_module, "_openai_singletons", {})
        monkeypatch.setattr(client_module.settings, "openai_api_key", None)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            client_module._get_openai_client()

    def test_uses_api_key_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client_module, "_openai_singletons", {})
        monkeypatch.setattr(client_module.settings, "openai_api_key", SecretStr("sk-test"))
        # Should not raise.
        c = client_module._get_openai_client()
        assert c is not None
