"""Thin wrapper around the Anthropic SDK + `instructor`.

Why a wrapper:
    - One place to set defaults (model, temperature, max_tokens)
    - One place to attach retry / rate-limit logic
    - One place to plug in caching
    - Stations call `call_llm(...)` and get a Pydantic object back

`instructor` is a small library that patches the Anthropic client so the
LLM is forced to return JSON matching a Pydantic schema. If it returns
malformed JSON, it raises — we never silently get bad data.
"""

from __future__ import annotations

from typing import TypeVar

import anthropic
import instructor
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from discovery.config.settings import settings

_RESP = TypeVar("_RESP", bound=BaseModel)

# A single shared async client. Anthropic recommends reusing one client.
_raw_client = anthropic.AsyncAnthropic(
    api_key=settings.anthropic_api_key.get_secret_value()
)
_client = instructor.from_anthropic(_raw_client)


@retry(
    retry=retry_if_exception_type(
        (anthropic.RateLimitError, anthropic.APIConnectionError)
    ),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def call_llm(
    *,
    system: str,
    user: str,
    response_model: type[_RESP],
    model: str = "claude-sonnet-4-5",
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> _RESP:
    """Run one LLM call with structured-output enforcement.

    Parameters
    ----------
    system : str
        The system prompt. Constant for a given station.
    user : str
        The rendered user message containing the batch + few-shot examples.
    response_model : type[BaseModel]
        A Pydantic model the response must conform to. If the LLM returns
        anything that fails validation, an exception is raised.
    model : str
        Anthropic model ID. Defaults to Sonnet 4.5.
    temperature : float
        0.0 for deterministic classification; raise only when you want
        variety.
    max_tokens : int
        Hard ceiling on response length.

    Returns
    -------
    The parsed Pydantic object.
    """
    return await _client.messages.create(  # type: ignore[no-any-return]
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
        response_model=response_model,
    )
