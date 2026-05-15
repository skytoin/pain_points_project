"""Provider-specific LLM call wrappers.

Two functions, one per provider:

    - `call_anthropic` — Messages API. `system` is a top-level param;
      `max_tokens` is required by the SDK.
    - `call_openai` — Chat Completions API. `system` is folded into the
      messages array as a `developer`-role entry (gpt-5.x renamed the
      system role; the SDK still accepts `"role": "system"` but
      `"developer"` is the modern spelling).

Both are wrapped by `instructor` so the model is forced to return JSON
matching a Pydantic schema. If validation fails, the call raises — we
never silently get bad data.

No facade `call_llm` exists. Callers import the provider they want.
"""

from __future__ import annotations

from typing import Any, cast

import anthropic
import instructor
import openai
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from discovery.config.settings import settings

# A single shared async Anthropic client — the SDK recommends reusing one.
_anthropic_raw = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
_anthropic_client = instructor.from_anthropic(_anthropic_raw)


# Lazy singleton — only instantiated when a station actually calls
# call_openai. Existing Anthropic-only flows keep booting without an
# OpenAI key. Stored in a dict so we mutate (rather than rebind) the
# module-level name and avoid the `global` keyword.
_openai_singletons: dict[str, Any] = {}


def _get_openai_client() -> Any:
    """Return a memoized instructor-wrapped AsyncOpenAI client.

    Raises RuntimeError if `OPENAI_API_KEY` isn't set — the caller (a
    station) is expected to catch this and fall back to a deterministic
    path.
    """
    if "client" in _openai_singletons:
        return _openai_singletons["client"]
    if settings.openai_api_key is None:
        raise RuntimeError(
            "OPENAI_API_KEY is not set; cannot call OpenAI station. "
            "Stations should fall back to a deterministic path."
        )
    raw = openai.AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    _openai_singletons["client"] = instructor.from_openai(raw)
    return _openai_singletons["client"]


@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def call_anthropic[Resp: BaseModel](
    *,
    system: str,
    user: str,
    response_model: type[Resp],
    model: str = "claude-sonnet-4-5",
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> Resp:
    """Call Anthropic Messages API via instructor.

    `system` is passed as Anthropic's top-level `system=` param.
    `max_tokens` is required by the SDK.
    """
    return await _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
        response_model=response_model,
    )


@retry(
    retry=retry_if_exception_type((openai.RateLimitError, openai.APIConnectionError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def call_openai[Resp: BaseModel](
    *,
    system: str,
    user: str,
    response_model: type[Resp],
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> Resp:
    """Call OpenAI Chat Completions API via instructor.

    Two gpt-5.x quirks the wrapper handles for callers:

      - `system` is folded into the messages array as a `developer`-role
        entry. The Chat Completions API has no top-level `system=` param,
        and gpt-5.x renamed the message role from `system` → `developer`.
      - `max_tokens` is renamed `max_completion_tokens` at the API level.
        Python callers still pass `max_tokens=...` for ergonomic
        consistency with `call_anthropic`; we translate at the boundary.

    `model` has no default — pick one explicitly at the call site.
    """
    client = _get_openai_client()
    # _get_openai_client returns Any (instructor's patched client has
    # no useful type stub). Cast back to Resp — instructor enforces the
    # response_model at runtime, so this matches reality.
    return cast(
        "Resp",
        await client.chat.completions.create(
            model=model,
            max_completion_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "developer", "content": system},
                {"role": "user", "content": user},
            ],
            response_model=response_model,
        ),
    )
