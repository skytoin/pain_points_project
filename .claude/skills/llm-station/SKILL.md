---
name: llm-station
description: Pattern and contract for writing an LLM "station" — Pydantic-validated, cached, batched, retryable. Read this whenever adding or modifying any LLM call.
---

# LLM Station Pattern

Every LLM call in this project follows the same eight-step contract.
"Station" just means "a step in the pipeline that uses the LLM." Five
stations are described in `docs/architecture.md`.

## The contract

```
1. Gather inputs (a batch of items)
2. Compute content_hash of (batch_bytes + prompt VERSION + model name)
3. Check cache — return cached if hit
4. Build prompt: system + few-shot + batch + Pydantic schema
5. Call LLM with temperature=0 and structured-output enforced via `instructor`
6. Pydantic validates the response automatically; malformed → exception
7. Write the validated result to the cache
8. Return validated objects to the worker
```

## File layout for one station

```
src/discovery/llm/
├── client.py            ← the shared LLM client wrapper (anthropic + instructor)
├── cache.py             ← diskcache-backed key/value store
├── schemas.py           ← Pydantic output schemas, ONE per station
├── prompts/
│   └── <station>.py     ← VERSION + SYSTEM_PROMPT + build_user_message()
└── stations.py          ← run_<station>(batch) functions
```

## Required pieces in a prompt file

```python
# src/discovery/llm/prompts/pain_extraction.py

VERSION: str = "v3"          # bump when you change the prompt

SYSTEM_PROMPT: str = """You are a careful analyst. ..."""

FEW_SHOT_EXAMPLES: list[dict] = [
    {"input": "...", "output": {...}},
]

def build_user_message(batch: list[PainInput]) -> str:
    """Render the batch and few-shot examples into the user message."""
    ...
```

## Required pieces in `stations.py`

```python
from discovery.llm.client import call_llm
from discovery.llm.cache import cache_key, get_cached, put_cached
from discovery.llm.schemas import PainExtraction
from discovery.llm.prompts import pain_extraction

MODEL = "claude-sonnet-4-5"   # or whatever the project standardizes on
TEMPERATURE = 0

async def run_pain_extraction(
    batch: list[PainInput],
) -> list[PainExtraction]:
    key = cache_key(
        batch=batch,
        prompt_version=pain_extraction.VERSION,
        model=MODEL,
    )
    if (cached := get_cached(key)) is not None:
        return cached

    results = await call_llm(
        system=pain_extraction.SYSTEM_PROMPT,
        user=pain_extraction.build_user_message(batch),
        response_model=list[PainExtraction],
        model=MODEL,
        temperature=TEMPERATURE,
    )
    put_cached(key, results)
    return results
```

## Hard rules

- **Never** parse free-form strings out of LLM responses. Use Pydantic.
- **Never** silently truncate a batch on the way to the LLM. If the batch
  is too big for the model context, split it into multiple cache-keyed
  sub-batches.
- **Never** auto-create canonical rows (in `companies`, `tools`, etc.) from
  LLM output. Those go to `*_unverified` queues for review.
- **Always** bump `VERSION` when you change the system prompt, schema, or
  few-shot examples. The cache key includes VERSION; old results stay
  cached but are no longer hit.
- **Always** test with a small fake batch + a mocked `call_llm` before
  running on real data.

## Cost knob

The default model is Sonnet. For stations that need higher accuracy
(Entity Resolution, Sanity Check), Opus may be worth the extra cost. For
mass classification (Pain Signal, Job-Task), Sonnet or Haiku is plenty.

If you switch models, the cache key changes and re-runs cost full price
again. Decide before you process a million rows.
