---
name: new-llm-station
description: Scaffold a new LLM station with Pydantic schema, versioned prompt, and cache
---

I want to add a new LLM station named `$ARGUMENTS`. Use the AskUserQuestion
tool ONCE to gather:

1. What is the input (one record, a batch, a free-form spec)?
2. What is the output? (Show me 2-3 candidate Pydantic field sets if I
   haven't decided.)
3. Should this be cached by content hash (yes for most stations)?
4. Batch size — items per LLM call?

After I answer, do this:

**Step 1 — Schema.** Add a Pydantic output model to
`src/discovery/llm/schemas.py`. Use `Literal` for closed enums, `Field`
constraints for numeric ranges, and concise docstrings.

**Step 2 — Prompt.** Create
`src/discovery/llm/prompts/$ARGUMENTS.py` containing:

- A `VERSION: str = "v1"` constant (the cache key uses it)
- A `SYSTEM_PROMPT: str` constant
- A `def build_user_message(batch: list[...]) -> str` function

Keep both prompts focused — give the model the schema + 1-2 short
examples + the batch. Nothing else.

**Step 3 — Station function.** Add a function in
`src/discovery/llm/stations.py` named `run_$ARGUMENTS(batch)` that:

- Computes a cache key from prompt VERSION + model name + batch hash
- Checks the disk cache (or `llm_cache` table)
- On miss: calls `discovery.llm.client.call_llm()` with the schema
- Validates the response (Pydantic does this automatically via
  `instructor`)
- Writes to cache
- Returns the validated objects

**Step 4 — Wire into worker.** Add a task-type handler in
`src/discovery/workers/llm_worker.py`.

**Step 5 — Test.** A unit test in `tests/unit/llm/test_$ARGUMENTS.py`
that:

- Mocks `discovery.llm.client.call_llm`
- Verifies the schema validates correct shapes
- Verifies a malformed response raises cleanly (not silently)

**Step 6 — Run `/run-checks`.**

Do NOT actually call the LLM during this scaffolding. Mock it in tests.
The first real call should happen later, on a tiny batch, with me watching.
