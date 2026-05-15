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

# A single shared async Anthropic client — the SDK recommends reusing one.
_anthropic_raw = anthropic.AsyncAnthropic(
    api_key=settings.anthropic_api_key.get_secret_value()
)
_anthropic_client = instructor.from_anthropic(_anthropic_raw)


@retry(
    retry=retry_if_exception_type(
        (anthropic.RateLimitError, anthropic.APIConnectionError)
    ),
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
