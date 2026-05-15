"""Tests for `discovery.llm.client.call_anthropic`.

We monkeypatch the module-level `_anthropic_client` so no real network
is hit. The point isn't to verify Anthropic's API works — it's to
verify our wrapper passes the right params and surfaces the right
return type.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from discovery.llm import client as client_module
from discovery.llm.client import call_anthropic


class _Echo(BaseModel):
    msg: str


class _FakeAnthropicMessages:
    def __init__(self) -> None:
        self.last_call: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> _Echo:
        self.last_call = kwargs
        return _Echo(msg="ok")


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = _FakeAnthropicMessages()


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> _FakeAnthropicClient:
    fake = _FakeAnthropicClient()
    monkeypatch.setattr(client_module, "_anthropic_client", fake)
    return fake


class TestCallAnthropic:
    async def test_returns_validated_pydantic(self, fake_anthropic: _FakeAnthropicClient) -> None:
        result = await call_anthropic(
            system="sys",
            user="usr",
            response_model=_Echo,
            model="claude-sonnet-4-5",
        )
        assert isinstance(result, _Echo)
        assert result.msg == "ok"

    async def test_passes_system_as_top_level_param(
        self, fake_anthropic: _FakeAnthropicClient
    ) -> None:
        """Anthropic uses a top-level `system=` param, not a messages entry."""
        await call_anthropic(
            system="you are a calculator",
            user="2+2?",
            response_model=_Echo,
            model="claude-sonnet-4-5",
        )
        call = fake_anthropic.messages.last_call
        assert call is not None
        assert call["system"] == "you are a calculator"
        assert call["messages"] == [{"role": "user", "content": "2+2?"}]

    async def test_passes_max_tokens(self, fake_anthropic: _FakeAnthropicClient) -> None:
        await call_anthropic(
            system="sys",
            user="usr",
            response_model=_Echo,
            model="claude-sonnet-4-5",
            max_tokens=2048,
        )
        call = fake_anthropic.messages.last_call
        assert call is not None
        assert call["max_tokens"] == 2048
