# Wave 0 — LLM Query Expansion Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development
> (if subagents available) or superpowers:executing-plans to implement this plan.
> Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **🛑 Before starting any task, read the skills it touches:**
> - Every task in Chunk 2, 3, 5 → read `.claude/skills/llm-station/SKILL.md`.
> - Every task touching `discovery.orchestrator.reddit*` or the validator →
>   read `.claude/skills/reddit-source/SKILL.md` (the items referenced by
>   number in this plan).
> - Touching anything under `src/discovery/sources/` → read
>   `.claude/skills/source-adapter/SKILL.md`. (This plan doesn't touch sources
>   but the rule still holds.)

**Goal:** Replace the hand-rolled Reddit query template with an LLM-built
query plan. One OpenAI `gpt-5.4` call per job produces 10–15 Reddit search
queries; a Python validator enforces the Reddit syntax rules; the result is
cached and stored on `Job.job_plan`; the existing template stays as the
deterministic fallback.

**Architecture:** Wave 0 runs **inline** as `plan_job(session, job)` between
`create_job` and `enqueue_reddit_task_for_job` in the CLI. The LLM call goes
through a new provider-specific `call_openai` function (split out from the
existing `call_llm`, which becomes `call_anthropic`). Cache is `diskcache`
keyed on `(spec, prompt VERSION, model)`. Fallback to the hand-rolled
template kicks in when the LLM call raises, validation drops too many
queries, or the OpenAI API key is missing.

**Tech stack:** Python 3.12, `openai>=1.50`, `anthropic` (kept), `instructor`,
`diskcache`, `pydantic` v2, `tenacity`, `SQLModel`. Provider-specific
functions, not a generic facade.

---

## Decision record — why Option A and not Option B

This slice runs Wave 0 **inline** inside the CLI's `discovery run` flow. The
alternative — make Wave 0 a worker task with `wave=0, source="llm:query_expansion"`
— was considered and deferred.

**Reasoning:**

- Speed in single-worker mode: A is ~20–90 ms faster per Wave 0 call, less
  than 1% of the cache-miss wall-clock and irrelevant in absolute terms.
- Wave 0 is one LLM call per job; the queue's retry / batching value isn't
  realized.
- A is ~60 lines including tests; B's three sub-options (B1 task-self-enqueues,
  B2 orchestrator polls, B3 job state machine) each add meaningful complexity.
- `run_query_expansion(spec) -> JobPlan` is provider-agnostic and orchestrator-
  agnostic; promotion to a task is a ~20-line wrapper if/when needed.

**When to revisit Option B:**

- If the project grows to multiple worker processes — running Wave 0 for N
  jobs in parallel becomes a real win.
- If a "discovery status" dashboard needs Wave 0 failures visible in the
  `tasks` table alongside everything else.
- If the cumulative orchestration overhead from running many jobs serially
  starts showing up in measurements — re-measure first; A's overhead is
  tiny per job, but it stacks linearly with job count.

**Promotion path:** wrap `plan_job(session, job)` in a worker task body:

```python
async def wave_0_task(session: AsyncSession, task: Task) -> None:
    job = await session.get(Job, task.job_id)
    await plan_job(session, job)
    # task body returns; worker marks done; a follow-up step
    # enqueues Wave 1 (this is the B1/B2/B3 design decision).
```

That's the whole conversion. Keep `plan_job` and `run_query_expansion`
pure of any queue concerns so this stays a 20-line wrapper.

---

## File map

```
src/discovery/
├── config/settings.py           ← MODIFY: add openai_api_key
├── llm/
│   ├── client.py                ← MODIFY: rename call_llm → call_anthropic;
│   │                              add call_openai (no facade)
│   ├── cache.py                 ← CREATE: cache_key / get_cached / put_cached
│   ├── schemas.py               ← CREATE: RedditQuerySpec + JobPlan
│   ├── prompts/
│   │   ├── __init__.py          ← CREATE (empty)
│   │   └── query_expansion.py   ← CREATE: VERSION + SYSTEM_PROMPT +
│   │                              FEW_SHOT_EXAMPLES + build_user_message
│   └── stations/
│       ├── __init__.py          ← CREATE (empty)
│       └── query_expansion.py   ← CREATE: run_query_expansion(spec)
└── orchestrator/
    ├── jobs.py                  ← CREATE: plan_job(session, job)
    ├── reddit.py                ← MODIFY: read from job.job_plan first,
    │                              fall back to template
    └── reddit_query_validator.py← CREATE: validate_reddit_query(spec)

cli/run.py                       ← MODIFY: call plan_job between create_job
                                    and enqueue_reddit_task_for_job

tests/unit/
├── llm/
│   ├── __init__.py
│   ├── test_cache.py            ← CREATE
│   ├── test_client_anthropic.py ← CREATE (renamed from any existing)
│   ├── test_client_openai.py    ← CREATE
│   ├── test_schemas.py          ← CREATE
│   └── stations/
│       ├── __init__.py
│       └── test_query_expansion.py ← CREATE
├── test_orchestrator_jobs.py    ← CREATE
├── test_orchestrator_reddit_query_validator.py  ← CREATE
├── test_orchestrator_reddit.py  ← MODIFY: cover new job_plan path
└── test_settings.py             ← CREATE (or modify if exists)

pyproject.toml                   ← PROPOSE: add openai>=1.50 (user applies)

.claude/skills/llm-station/SKILL.md  ← MODIFY: addendum on per-station
                                        provider/model/temperature choice

docs/handoff.md                  ← MODIFY: Wave 0 done + future-B note
```

---

## Chunk 1: Settings + dependency

The OpenAI SDK and the API key. Two small pieces that block everything
else.

### Task 1.1: Propose adding `openai` to `pyproject.toml`

CLAUDE.md says the user applies `pyproject.toml` changes. Don't run
`uv add openai` yourself.

**Files:**
- Propose change to: `pyproject.toml` (line ~33, the LLM section)

- [ ] **Step 1: Open `pyproject.toml` and locate the LLM dependency block**

Currently:
```toml
    # LLM
    "anthropic>=0.34",
    "instructor>=1.4",
    "tiktoken>=0.7",
    "diskcache>=5.6",
```

- [ ] **Step 2: Tell the user the exact change to apply**

Output to the user:

> Please add `"openai>=1.50",` to the LLM dependency group in
> `pyproject.toml` (right after `"anthropic>=0.34",`), then run
> `uv sync`. Confirm when done.

- [ ] **Step 3: After user confirms, verify the dep is installed**

Run: `uv run python -c "import openai; print(openai.__version__)"`
Expected: prints `1.x.x` (>=1.50). If the import fails, ask the user
to re-run `uv sync` and try again.

- [ ] **Step 4: Commit the proposal (after user applies)**

`pyproject.toml` and `uv.lock` should both reflect the change. The
user's commit is fine; if you commit, message it:

```
chore(deps): add openai>=1.50 for Wave 0 query expansion station
```

### Task 1.2: Add `openai_api_key` to `Settings`

The key is optional (so existing Anthropic-only flows keep booting). The
Wave 0 station fails cleanly into the fallback when the key is absent.

**Files:**
- Modify: `src/discovery/config/settings.py:42-43` (the `# ---- LLM ----`
  block, right under `anthropic_api_key`)
- Test: `tests/unit/test_settings.py` (create if missing)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_settings.py`:

```python
"""Tests for `discovery.config.settings`.

Settings are read from env vars; tests use `monkeypatch.setenv` rather
than touching the real `.env` file.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from discovery.config.settings import Settings


class TestSettings:
    def test_openai_api_key_is_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent OPENAI_API_KEY → field is None; existing flows keep booting."""
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
```

- [ ] **Step 2: Run the test, confirm it fails**

`uv run pytest tests/unit/test_settings.py -v`
Expected: `AttributeError` or `ValidationError` because the field
doesn't exist yet.

- [ ] **Step 3: Add the field to `Settings`**

In `src/discovery/config/settings.py`, after the `anthropic_api_key`
line:

```python
    # ---- LLM -------------------------------------------------------
    anthropic_api_key: SecretStr
    openai_api_key: SecretStr | None = None
```

Keep it optional — the Wave 0 station's fallback handles the
None case.

- [ ] **Step 4: Run tests, confirm pass**

`uv run pytest tests/unit/test_settings.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_settings.py src/discovery/config/settings.py
git commit -m "feat(config): add optional OPENAI_API_KEY setting"
```

---

## Chunk 2: Cache module

The `llm-station` skill calls for `cache.py` in the file layout. No
station can be cache-compliant without it, so it ships in this slice.

### Task 2.1: Create the cache module

Three small helpers over `diskcache.Cache`, typed by Pydantic model
class so callers get a real return type, not `Any`.

**Files:**
- Create: `src/discovery/llm/cache.py`
- Create: `src/discovery/llm/__init__.py` (if it doesn't already re-export,
  add nothing — it stays an empty marker for now)
- Test: `tests/unit/llm/__init__.py` (empty), `tests/unit/llm/test_cache.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/llm/__init__.py` (empty).

Create `tests/unit/llm/test_cache.py`:

```python
"""Tests for `discovery.llm.cache` — diskcache wrapper with typed get/put."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from discovery.llm.cache import cache_key, get_cached, make_cache, put_cached


class _Sample(BaseModel):
    a: int
    b: str


@pytest.fixture
def tmp_cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


class TestCacheKey:
    def test_key_is_deterministic(self) -> None:
        k1 = cache_key(spec={"x": 1}, prompt_version="v1", model="m")
        k2 = cache_key(spec={"x": 1}, prompt_version="v1", model="m")
        assert k1 == k2
        assert len(k1) == 64  # sha256 hex digest

    def test_key_changes_with_prompt_version(self) -> None:
        k1 = cache_key(spec={"x": 1}, prompt_version="v1", model="m")
        k2 = cache_key(spec={"x": 1}, prompt_version="v2", model="m")
        assert k1 != k2

    def test_key_changes_with_model(self) -> None:
        k1 = cache_key(spec={"x": 1}, prompt_version="v1", model="m1")
        k2 = cache_key(spec={"x": 1}, prompt_version="v1", model="m2")
        assert k1 != k2

    def test_key_is_order_independent_in_spec(self) -> None:
        """hash_params uses sort_keys=True — input dict ordering shouldn't matter."""
        k1 = cache_key(spec={"a": 1, "b": 2}, prompt_version="v", model="m")
        k2 = cache_key(spec={"b": 2, "a": 1}, prompt_version="v", model="m")
        assert k1 == k2


class TestRoundTrip:
    def test_put_then_get_returns_equivalent_model(self, tmp_cache_dir: Path) -> None:
        cache = make_cache(tmp_cache_dir)
        key = cache_key(spec={"x": 1}, prompt_version="v", model="m")
        put_cached(cache, key, _Sample(a=1, b="hi"))
        got = get_cached(cache, key, _Sample)
        assert got is not None
        assert got.a == 1
        assert got.b == "hi"

    def test_miss_returns_none(self, tmp_cache_dir: Path) -> None:
        cache = make_cache(tmp_cache_dir)
        got = get_cached(cache, "no-such-key", _Sample)
        assert got is None

    def test_stored_model_revalidates_on_read(self, tmp_cache_dir: Path) -> None:
        """We store JSON, not pickles — read path validates through Pydantic."""
        cache = make_cache(tmp_cache_dir)
        key = "k"
        # store a JSON string that would round-trip through _Sample
        put_cached(cache, key, _Sample(a=42, b="x"))
        got = get_cached(cache, key, _Sample)
        assert isinstance(got, _Sample)
```

- [ ] **Step 2: Run the tests, confirm they fail**

`uv run pytest tests/unit/llm/test_cache.py -v`
Expected: ImportError — `discovery.llm.cache` doesn't exist yet.

- [ ] **Step 3: Implement `cache.py`**

Create `src/discovery/llm/cache.py`:

```python
"""Diskcache-backed cache for LLM station outputs.

Why JSON not pickle:
    Pydantic models pickle, but we store the model's JSON representation
    instead. JSON survives unrelated code changes (e.g. moving a model
    to a new module) where pickles would break. The cache key already
    encodes prompt VERSION and model, so a schema change should bump
    one of those and miss the cache anyway — but JSON is safer if
    someone forgets.

Public surface
--------------
- `cache_key(**parts) -> str` — sha256 hex digest of canonical JSON
  over the kwargs. Use named kwargs (`spec=`, `prompt_version=`,
  `model=`) for readability.
- `get_cached(cache, key, model) -> Model | None` — returns a
  validated Pydantic model or None on miss.
- `put_cached(cache, key, value) -> None` — stores `value.model_dump_json()`.
- `make_cache(dir) -> Cache` — open a `diskcache.Cache` rooted at `dir`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from diskcache import Cache
from pydantic import BaseModel

from discovery.hashing import hash_params


def cache_key(**parts: Any) -> str:
    """Canonical-JSON sha256 of the keyword arguments.

    Use named keys so the caller side is readable:

        cache_key(spec=spec.model_dump(mode="json"),
                  prompt_version=prompts.query_expansion.VERSION,
                  model="gpt-5.4")
    """
    return hash_params(parts)


def make_cache(directory: Path) -> Cache:
    """Open (or create) a diskcache at `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    return Cache(str(directory))


def get_cached[T: BaseModel](cache: Cache, key: str, model: type[T]) -> T | None:
    """Return a validated `model` instance for `key`, or None on miss.

    Cache stores JSON strings; we re-validate on read so a stored value
    that no longer matches the model raises cleanly (and the caller can
    fall through to a fresh LLM call).
    """
    raw = cache.get(key)
    if raw is None:
        return None
    return model.model_validate_json(raw)


def put_cached(cache: Cache, key: str, value: BaseModel) -> None:
    """Store `value.model_dump_json()` under `key`."""
    cache.set(key, value.model_dump_json())
```

- [ ] **Step 4: Run the tests, confirm they pass**

`uv run pytest tests/unit/llm/test_cache.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Run lint and types**

```
uv run ruff check --fix src/discovery/llm/cache.py tests/unit/llm/test_cache.py
uv run mypy src/discovery/llm/cache.py
```
Expected: no errors. If diskcache stubs are missing, `pyproject.toml` already
has `diskcache.*` in `ignore_missing_imports`.

- [ ] **Step 6: Commit**

```bash
git add src/discovery/llm/cache.py tests/unit/llm/__init__.py tests/unit/llm/test_cache.py
git commit -m "feat(llm): add diskcache wrapper (cache_key/get/put)"
```

---

## Chunk 3: Provider-specific LLM client functions

Split `call_llm` into two functions, one per provider. No generic facade.
Each function knows its own SDK's quirks:

- Anthropic: `system` is a top-level param; `max_tokens` is required.
- OpenAI: `system` is folded into `messages` as a `developer`-role entry
  (gpt-5.x renamed `system` → `developer`).

Both retry their own SDK's `RateLimitError` and `APIConnectionError`.

### Task 3.1: Rename `call_llm` → `call_anthropic`

No callers exist yet (confirmed by `Grep "call_llm" src/` returning only
the definition), so this is a pure rename + tests.

**Files:**
- Modify: `src/discovery/llm/client.py`
- Test: `tests/unit/llm/test_client_anthropic.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/llm/test_client_anthropic.py`:

```python
"""Tests for `discovery.llm.client.call_anthropic`.

We monkeypatch the module-level `_anthropic_client` so no real network
is hit. The point of these tests isn't to verify Anthropic's API works
— it's to verify our wrapper passes the right params and surfaces the
right return type.
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
    async def test_returns_validated_pydantic(
        self, fake_anthropic: _FakeAnthropicClient
    ) -> None:
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
        # user prompt goes into messages array
        assert call["messages"] == [{"role": "user", "content": "2+2?"}]

    async def test_passes_max_tokens(
        self, fake_anthropic: _FakeAnthropicClient
    ) -> None:
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
```

- [ ] **Step 2: Run the test, confirm it fails**

`uv run pytest tests/unit/llm/test_client_anthropic.py -v`
Expected: ImportError on `call_anthropic` — it doesn't exist yet.

- [ ] **Step 3: Rename in `client.py`**

In `src/discovery/llm/client.py`:

- Rename the variable `_client` → `_anthropic_client`.
- Rename the function `call_llm` → `call_anthropic`.
- Update the docstring to clarify it's Anthropic-specific.

Result:

```python
"""Provider-specific LLM call wrappers.

Two functions, one per provider:

    - `call_anthropic` — Messages API. `system` is a top-level param;
      `max_tokens` is required by the SDK.
    - `call_openai` — Chat Completions API. `system` goes into the
      messages array as a `developer`-role entry (gpt-5.x renamed
      the system role; the SDK still accepts `"role": "system"` but
      `"developer"` is the modern spelling).

Both are wrapped by `instructor` so the model is forced to return JSON
matching a Pydantic schema. If validation fails, the call raises —
we never silently get bad data.

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
```

(Note: `call_openai` follows in Task 3.2 — leave a stub for now if you
want, or skip until Task 3.2.)

- [ ] **Step 4: Run the tests, confirm pass**

`uv run pytest tests/unit/llm/test_client_anthropic.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Run lint, types**

```
uv run ruff check --fix src/discovery/llm/client.py tests/unit/llm/
uv run mypy src/discovery/llm/client.py
```

- [ ] **Step 6: Commit**

```bash
git add src/discovery/llm/client.py tests/unit/llm/test_client_anthropic.py
git commit -m "refactor(llm): rename call_llm to call_anthropic (provider split prep)"
```

### Task 3.2: Add `call_openai`

The differences from Anthropic: system goes in the messages array as a
`developer` role entry; the SDK is `openai.AsyncOpenAI`; exceptions
live under the `openai` module. The retry decorator lists OpenAI's
exception classes only — they're not interchangeable with Anthropic's.

The OpenAI client is lazy: we only instantiate it when called, so that
flows without `OPENAI_API_KEY` set keep booting.

**Files:**
- Modify: `src/discovery/llm/client.py`
- Test: `tests/unit/llm/test_client_openai.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/llm/test_client_openai.py`:

```python
"""Tests for `discovery.llm.client.call_openai`.

Monkeypatch the lazy client getter so no real network is hit. Verify the
OpenAI-specific quirks: system goes into messages as `developer` role,
and the lazy initialization respects a None API key.
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
    async def test_returns_validated_pydantic(
        self, fake_openai: _FakeOpenAIClient
    ) -> None:
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

    async def test_passes_model_and_temperature(
        self, fake_openai: _FakeOpenAIClient
    ) -> None:
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

    def test_raises_when_api_key_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lazy init: if openai_api_key is None, building the client raises a
        clear error so the station can fall back."""
        # ensure the cached client (if any) is cleared
        monkeypatch.setattr(client_module, "_openai_client_singleton", None)
        monkeypatch.setattr(client_module.settings, "openai_api_key", None)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            client_module._get_openai_client()

    def test_uses_api_key_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "_openai_client_singleton", None)
        monkeypatch.setattr(
            client_module.settings, "openai_api_key", SecretStr("sk-test")
        )
        # Should not raise.
        c = client_module._get_openai_client()
        assert c is not None
```

- [ ] **Step 2: Run the test, confirm it fails**

`uv run pytest tests/unit/llm/test_client_openai.py -v`
Expected: ImportError on `call_openai`.

- [ ] **Step 3: Implement `call_openai`**

Append to `src/discovery/llm/client.py`:

```python
import openai

# Lazy singleton — we only instantiate the OpenAI client when a station
# actually calls call_openai. That way, flows that don't use OpenAI
# (every existing Anthropic station) keep booting without an API key.
_openai_client_singleton: Any = None


def _get_openai_client() -> Any:
    """Return a memoized instructor-wrapped AsyncOpenAI client.

    Raises RuntimeError if `OPENAI_API_KEY` isn't set — the caller (a
    station) is expected to catch this and fall back to a deterministic
    path.
    """
    global _openai_client_singleton
    if _openai_client_singleton is not None:
        return _openai_client_singleton
    if settings.openai_api_key is None:
        raise RuntimeError(
            "OPENAI_API_KEY is not set; cannot call OpenAI station. "
            "Stations should fall back to a deterministic path."
        )
    raw = openai.AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    _openai_client_singleton = instructor.from_openai(raw)
    return _openai_client_singleton


@retry(
    retry=retry_if_exception_type(
        (openai.RateLimitError, openai.APIConnectionError)
    ),
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

    `system` is folded into the messages array as a `developer`-role
    entry — gpt-5.x renamed the system role; the new spelling is
    `developer`. No `system=` top-level param exists on this API.

    `model` has no default — pick one explicitly at the call site.
    """
    client = _get_openai_client()
    return await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "developer", "content": system},
            {"role": "user", "content": user},
        ],
        response_model=response_model,
    )
```

Add `from typing import Any` near the top if not present.

- [ ] **Step 4: Run the tests, confirm pass**

`uv run pytest tests/unit/llm/test_client_openai.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Run all client tests + lint + types**

```
uv run pytest tests/unit/llm/ -v
uv run ruff check --fix src/discovery/llm/client.py
uv run mypy src/discovery/llm/client.py
```
Expected: 8 PASS (3 anthropic + 5 openai), lint clean, mypy clean.

- [ ] **Step 6: Commit**

```bash
git add src/discovery/llm/client.py tests/unit/llm/test_client_openai.py
git commit -m "feat(llm): add call_openai (provider-specific, lazy client)"
```

---

## Chunk 4: Schemas — RedditQuerySpec and JobPlan

The LLM emits one `JobPlan` per call. It contains 10–15 `RedditQuerySpec`
objects (one per Reddit search) plus a list of LLM-picked subreddits.
The schema is permissive (`extra="allow"`) so the same prompt can be
extended later to emit YouTube/News/Apollo fields without a code change
— but those extra fields won't be readable from app code until a typed
field is added.

### Task 4.1: Create `RedditQuerySpec` and `JobPlan`

**Files:**
- Create: `src/discovery/llm/schemas.py`
- Test: `tests/unit/llm/test_schemas.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/llm/test_schemas.py`:

```python
"""Tests for `discovery.llm.schemas` — RedditQuerySpec, JobPlan."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from discovery.llm.schemas import JobPlan, RedditQuerySpec


def _good_query(q: str = '"I would pay"') -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint="site_wide",
        q=q,
        sort="top",
        t="month",
        limit=100,
        rationale="picks high-signal posts on willingness to pay",
    )


class TestRedditQuerySpec:
    def test_minimal_valid_spec(self) -> None:
        spec = _good_query()
        assert spec.endpoint == "site_wide"
        assert spec.sort == "top"

    def test_q_has_min_length(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="site_wide", q="", rationale="x"
            )

    def test_q_has_max_length(self) -> None:
        """Schema caps q at 3900 chars — Pydantic-level early rejection of URL-busters."""
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="site_wide", q="x" * 3901, rationale="x"
            )

    def test_rationale_is_required(self) -> None:
        """Forces the LLM to explain itself — improves quality, logged for debugging."""
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="site_wide", q="x", rationale=""
            )

    def test_endpoint_must_be_one_of_two(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="bogus", q="x", rationale="x"  # type: ignore[arg-type]
            )

    def test_limit_clamped_to_1_100(self) -> None:
        with pytest.raises(ValidationError):
            RedditQuerySpec(
                endpoint="site_wide", q="x", limit=101, rationale="x"
            )


class TestJobPlan:
    def test_requires_min_10_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(9)])

    def test_accepts_10_queries(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(10)])
        assert len(plan.reddit_queries) == 10

    def test_rejects_more_than_15_queries(self) -> None:
        with pytest.raises(ValidationError):
            JobPlan(reddit_queries=[_good_query() for _ in range(16)])

    def test_extra_fields_round_trip(self) -> None:
        """extra='allow' — future prompts can emit extra fields and they
        stay on the model (and on Job.job_plan JSON) without losing them."""
        plan = JobPlan.model_validate(
            {
                "reddit_queries": [_good_query().model_dump() for _ in range(10)],
                "youtube_queries": ["a", "b"],  # not a typed field yet
            }
        )
        # Round-trips back out
        dumped = plan.model_dump()
        assert "youtube_queries" in dumped
        assert dumped["youtube_queries"] == ["a", "b"]

    def test_reddit_subreddits_defaults_to_empty(self) -> None:
        plan = JobPlan(reddit_queries=[_good_query() for _ in range(10)])
        assert plan.reddit_subreddits == []
```

- [ ] **Step 2: Run the test, confirm it fails**

`uv run pytest tests/unit/llm/test_schemas.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement schemas**

Create `src/discovery/llm/schemas.py`:

```python
"""Pydantic schemas for LLM station outputs.

One model per station's output, plus shared sub-models. The output of
the Wave 0 (Query Expansion) station is `JobPlan`.

NOTE TO FUTURE SESSIONS
-----------------------
`JobPlan` uses `extra="allow"` so future prompts can emit additional
source fields (`youtube_queries`, `news_keywords`, `apollo_params`,
etc.) and they will round-trip through `Job.job_plan` without changes
here. BUT — to actually CONSUME those fields in app code (e.g. wire
YouTube queries into a YouTubeSource adapter), you MUST add a typed
field on this model AND wire the orchestrator to read from it. Don't
reach into `plan.model_extra["youtube_queries"]` from app code; that's
a bug-magnet because the field isn't validated. Add the field, then
use it.

The fields below are the only ones Wave 0 needs today: Reddit-shaped
because Reddit is the only source built so far.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RedditQuerySpec(BaseModel):
    """One LLM-built Reddit search query.

    The LLM fills `q` with a complete OR-compressed Reddit search string
    per the rules in `.claude/skills/reddit-source/SKILL.md` (items 6,
    7, 8, 10, 12, 13). Python validation in
    `discovery.orchestrator.reddit_query_validator` catches the rules
    the LLM might still slip on (uppercase operators, URL ceiling,
    valid subreddit names). Queries that don't pass validation are
    dropped before being sent to Reddit.
    """

    model_config = ConfigDict(frozen=True)

    endpoint: Literal["per_sub", "site_wide"]
    q: str = Field(min_length=1, max_length=3900)
    sort: Literal["top", "hot", "new"] = "top"
    t: Literal["hour", "day", "week", "month", "year", "all"] = "month"
    limit: int = Field(default=100, ge=1, le=100)
    rationale: str = Field(
        min_length=1,
        description=(
            "Why this query is worth running. Forces the LLM to "
            "explain itself; logged with the query for debugging "
            "bad plans."
        ),
    )


class JobPlan(BaseModel):
    """LLM-produced query plan for one Job. Wave 0's output.

    See module docstring for why `extra="allow"` and how future
    sessions should extend it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    reddit_queries: list[RedditQuerySpec] = Field(min_length=10, max_length=15)
    reddit_subreddits: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run the test, confirm pass**

`uv run pytest tests/unit/llm/test_schemas.py -v`
Expected: 11 PASS.

- [ ] **Step 5: Lint + types**

```
uv run ruff check --fix src/discovery/llm/schemas.py tests/unit/llm/test_schemas.py
uv run mypy src/discovery/llm/schemas.py
```

- [ ] **Step 6: Commit**

```bash
git add src/discovery/llm/schemas.py tests/unit/llm/test_schemas.py
git commit -m "feat(llm): add RedditQuerySpec + JobPlan schemas"
```

---

## Chunk 5: Query expansion prompt + validator + station

The three pieces of the Wave 0 station itself, in order:

1. **The prompt** (`prompts/query_expansion.py`) — system prompt, few-shot
   examples, user-message builder. This is where the Reddit skill rules
   become natural-language guidance for the LLM.
2. **The validator** (`orchestrator/reddit_query_validator.py`) — pure
   Python checks for the rules the LLM might still slip on. Returns a list
   of violations per query; queries with any violation are dropped.
3. **The station** (`stations/query_expansion.py`) — `run_query_expansion(spec)`
   ties cache + LLM call + validation + fallback-trigger together.

### Task 5.1: Build the query expansion prompt

Long system prompt, 2 few-shot examples, `build_user_message(spec)`.

**Files:**
- Create: `src/discovery/llm/prompts/__init__.py` (empty)
- Create: `src/discovery/llm/prompts/query_expansion.py`
- Test: `tests/unit/llm/test_prompts_query_expansion.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/llm/test_prompts_query_expansion.py`:

```python
"""Tests for `discovery.llm.prompts.query_expansion` — the Wave 0 prompt.

These tests pin the *shape* of the prompt module (VERSION present,
build_user_message renders the spec, system prompt mentions the key
Reddit syntax rules). They don't pin the prompt wording — that's
expected to evolve via VERSION bumps.
"""

from __future__ import annotations

from datetime import date

from discovery.jobs import JobSpec
from discovery.llm.prompts import query_expansion as qe


class TestPromptModule:
    def test_has_version(self) -> None:
        assert isinstance(qe.VERSION, str)
        assert qe.VERSION
        # cache key includes VERSION — bump it when prompt changes
        assert qe.VERSION.startswith("v")

    def test_system_prompt_mentions_core_reddit_rules(self) -> None:
        sp = qe.SYSTEM_PROMPT
        # Skill items the LLM must respect, in plain English in the prompt:
        assert "OR" in sp  # item 6: uppercase operators
        assert "subreddit:" in sp  # item 6: scope-to-sub syntax
        assert "quote" in sp.lower() or "quoted" in sp.lower()  # item 6
        assert "rationale" in sp.lower()  # the model has to explain itself
        assert "10" in sp and "15" in sp  # min/max queries

    def test_few_shot_examples_are_present(self) -> None:
        assert len(qe.FEW_SHOT_EXAMPLES) >= 2
        for ex in qe.FEW_SHOT_EXAMPLES:
            assert "input" in ex
            assert "output" in ex
            # output must structurally match JobPlan
            assert "reddit_queries" in ex["output"]
            assert len(ex["output"]["reddit_queries"]) >= 10


class TestBuildUserMessage:
    def test_renders_spec_fields(self) -> None:
        spec = JobSpec(
            industry="commercial cleaning",
            as_of=date(2026, 6, 1),
            location="NY",
            size="medium",
        )
        msg = qe.build_user_message(spec)
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg
        assert "2026-06-01" in msg

    def test_handles_optional_fields(self) -> None:
        spec = JobSpec(industry="bakery", as_of=date(2026, 6, 1))
        msg = qe.build_user_message(spec)
        assert "bakery" in msg
        # location and size are None — should not crash, should not
        # render "None" as a literal
        assert "None" not in msg
```

- [ ] **Step 2: Run the test, confirm fails**

`uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v`
Expected: ImportError.

- [ ] **Step 3: Build the prompt module**

Create `src/discovery/llm/prompts/__init__.py` (empty file).

Create `src/discovery/llm/prompts/query_expansion.py`:

```python
"""System prompt and helpers for the Wave 0 Query Expansion station.

The LLM (OpenAI gpt-5.4) sees this prompt plus a rendered user message
describing the JobSpec, and returns a `JobPlan` validated against the
Pydantic schema in `discovery.llm.schemas`.

Bumping VERSION
---------------
Bump VERSION whenever the system prompt, few-shot examples, or the
intended schema changes. The cache key includes VERSION; old results
stay in cache but are no longer hit (a fresh call is forced).

Versioning:
    v1 — initial release. gpt-5.4, 10–15 reddit_queries, structured
    `RedditQuerySpec` with rationale-per-query.
"""

from __future__ import annotations

from typing import Any

from discovery.jobs import JobSpec

VERSION: str = "v1"


SYSTEM_PROMPT: str = """\
You are a Reddit search query designer. Your job is to brainstorm
between 10 and 15 high-signal Reddit search queries for a given
industry, plus a short list of domain-specific subreddits worth
scanning.

These queries are aimed at finding posts where real practitioners
discuss pain points, willingness to pay, frustration, unmet needs,
and adjacent signals in this industry. Each query you produce will
be executed against Reddit's search API.

# How Reddit search works

You can search site-wide or scope to a single subreddit. The two endpoints
correspond to the `endpoint` field on each query you emit:

- `per_sub` — searches inside one specific subreddit. The query string
  should NOT include a `subreddit:` clause; the subreddit is implied
  by the endpoint. Use this for high-value niche subs.
- `site_wide` — searches across all of Reddit. The query string MUST
  include one or more `subreddit:NAME` clauses joined with `OR` to scope
  the search; otherwise you'll get noise from all of Reddit.

# Reddit search query syntax — the rules you MUST follow

1. **Quote multi-word phrases.** `"I would pay"` matches the literal
   phrase. Without quotes, Reddit splits it into separate word matches
   and you lose ~70% of real signals.

2. **OR / AND must be UPPERCASE.** Lowercase `or` is just a word.

3. **Parenthesize aggressively.** Make precedence explicit:
   `(subreddit:a OR subreddit:b) AND ("phrase1" OR "phrase2")`.

4. **Subreddit names: 3–21 chars, ASCII letters/digits/underscore only.**
   No spaces, no hyphens, no slashes. Strip any leading `r/`.
   Invalid examples that will be rejected: `"r/Small Business"`,
   `"AI/ML"`, `"my-sub"`.

5. **Cap subreddits per site_wide query.** Up to ~6 in one OR-clause
   per query. More than that blows past Reddit's ~4 KB URL ceiling.

6. **Cap pain-phrase variants.** Each pain category should have 3–4
   close paraphrases, OR'd together. Longer lists dilute precision
   and bloat the URL.

7. **Total query length must stay under 3900 characters.**

# Pain-phrase categories worth combining (ranked by signal strength)

These are guidelines, not a fixed list. Brainstorm beyond them where
it makes sense — but each query should be built around one CATEGORY
of pain expression, not a single keyword. Variants are PARAPHRASES,
not synonyms. `"I would buy"` is NOT a variant of `"I would pay"`
(one-time purchase vs. recurring willingness).

1. Willingness to pay: `"I would pay"`, `"I'd pay"`, `"would pay for"`
2. Unmet need: `"wish there was"`, `"wish someone would"`
3. Frustration: `"frustrated with"`, `"fed up with"`, `"tired of"`
4. Looking for alternatives: `"alternative to"`, `"replacement for"`
5. Market gap: `"why is there no"`, `"why does no one"`
6. Builder signals: `"built a tool"`, `"made a tool"`
7. Switching: `"switched from"`, `"moving away from"`
8. Dead competitor signals: `"shut down"`, `"killed off"`

# How to combine subreddit choice with phrase choice

Subreddits give you the DOMAIN; phrases give you the SIGNAL. A nurse
looking for product ideas searches the same phrases a DevOps founder
uses — just in different subs. Don't try to make domain-specific
phrase lists; you'll lose generality.

For each query, you choose:

- Which subreddits to scope to (1 for per_sub; 1–6 for site_wide)
- Which pain category and variants to OR together
- Whether to anchor on the industry literal (e.g. `"commercial cleaning"`)

# What to emit

You will emit a JSON object validated as `JobPlan` with two fields:

- `reddit_queries` — between 10 and 15 `RedditQuerySpec` objects.
  Each has `endpoint`, `q`, `sort`, `t`, `limit`, and a one-sentence
  `rationale` explaining why this query is worth running.
- `reddit_subreddits` — your shortlist of domain-relevant subreddits
  (without the `r/` prefix). Up to ~12. These complement the queries
  themselves; Python code may use this list to seed per-sub queries
  or rank subs for follow-up.

Each `rationale` is mandatory and visible to the engineer reviewing
plans. Be concrete: "scopes to nurse community for willingness-to-pay
signals on documentation tools" beats "looking for pain".

# Defaults

- `sort=top` unless you have a specific reason (`new` for emerging
  trends; `hot` for current discussion).
- `t=month` unless the spec's `as_of` date implies a narrower window.
- `limit=100` — it's the max Reddit allows; smaller wastes rate budget.

# What NOT to do

- Don't repeat near-identical queries. Each one should pull a
  meaningfully different slice.
- Don't put more than ~6 subreddits in a single site_wide query.
- Don't write pain phrases without quotes — Reddit will treat the
  words separately.
- Don't return fewer than 10 or more than 15 queries.
- Don't invent subreddits that obviously won't exist (e.g.
  `r/commercialcleaning2026`); stick to names that real communities
  actually use.
"""


FEW_SHOT_EXAMPLES: list[dict[str, Any]] = [
    {
        "input": {
            "industry": "commercial cleaning",
            "as_of": "2026-06-01",
            "location": "NY",
            "size": "medium",
        },
        "output": {
            "reddit_queries": [
                {
                    "endpoint": "site_wide",
                    "q": (
                        "(subreddit:smallbusiness OR subreddit:Entrepreneur OR "
                        "subreddit:startups) AND \"commercial cleaning\" AND "
                        "(\"I would pay\" OR \"I'd pay\" OR \"would pay for\")"
                    ),
                    "sort": "top",
                    "t": "month",
                    "limit": 100,
                    "rationale": (
                        "Cross-sub willingness-to-pay scan anchored on the "
                        "industry literal; baseline business subs only."
                    ),
                },
                {
                    "endpoint": "per_sub",
                    "q": "\"commercial cleaning\" AND (\"frustrated with\" OR \"fed up with\")",
                    "sort": "top",
                    "t": "month",
                    "limit": 100,
                    "rationale": (
                        "Scoped to r/CleaningTips for frustration signals from "
                        "actual practitioners."
                    ),
                },
                # ... (the real example includes 10–15; abbreviated here for the plan)
            ],
            "reddit_subreddits": [
                "CleaningTips",
                "Janitorial",
                "smallbusiness",
                "Entrepreneur",
                "OfficeCleaners",
            ],
        },
    },
    {
        "input": {"industry": "indie game development", "as_of": "2026-06-01"},
        "output": {
            "reddit_queries": [
                {
                    "endpoint": "site_wide",
                    "q": (
                        "(subreddit:gamedev OR subreddit:IndieDev OR "
                        "subreddit:Unity3D) AND (\"wish there was\" OR "
                        "\"wish someone would\")"
                    ),
                    "sort": "top",
                    "t": "month",
                    "limit": 100,
                    "rationale": (
                        "Unmet-need scan across the three biggest indie gamedev "
                        "subs; no industry anchor because the subs themselves "
                        "scope the domain."
                    ),
                },
                # ... (10–15 total in the real prompt)
            ],
            "reddit_subreddits": ["gamedev", "IndieDev", "Unity3D", "Godot", "gamedesign"],
        },
    },
]


def build_user_message(spec: JobSpec) -> str:
    """Render the JobSpec into a user message the LLM sees.

    Includes only the fields that are set (location and size are
    optional). The `as_of` date is rendered as ISO format so the LLM
    can reason about a coarse `t` parameter choice.
    """
    lines: list[str] = []
    lines.append(f"Industry: {spec.industry}")
    lines.append(f"As of: {spec.as_of.isoformat()}")
    if spec.location is not None:
        lines.append(f"Location: {spec.location}")
    if spec.size is not None:
        lines.append(f"Company size: {spec.size}")
    lines.append("")
    lines.append(
        "Produce a JobPlan with 10–15 reddit_queries and a shortlist "
        "of reddit_subreddits for this industry. Follow the system-"
        "prompt rules; explain each query's rationale."
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests, confirm pass**

`uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Lint + types**

```
uv run ruff check --fix src/discovery/llm/prompts/
uv run mypy src/discovery/llm/prompts/
```
Check that the file is under 600 lines (it should be ~250).

- [ ] **Step 6: Commit**

```bash
git add src/discovery/llm/prompts/ tests/unit/llm/test_prompts_query_expansion.py
git commit -m "feat(llm): add Wave 0 query expansion prompt module (v1)"
```

### Task 5.2: Build the Reddit query validator

A pure function that takes a `RedditQuerySpec` and returns a list of
skill-rule violations. The station drops invalid queries before
returning the `JobPlan`. Tests verify each skill item the validator is
responsible for.

**Files:**
- Create: `src/discovery/orchestrator/reddit_query_validator.py`
- Test: `tests/unit/test_orchestrator_reddit_query_validator.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_orchestrator_reddit_query_validator.py`:

```python
"""Tests for `discovery.orchestrator.reddit_query_validator`.

Each test pins one skill rule from
`.claude/skills/reddit-source/SKILL.md`. The validator returns a list
of violation strings — empty list means valid.
"""

from __future__ import annotations

from discovery.llm.schemas import RedditQuerySpec
from discovery.orchestrator.reddit_query_validator import validate_reddit_query


def _spec(q: str, endpoint: str = "site_wide", **kw: object) -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint=endpoint,  # type: ignore[arg-type]
        q=q,
        rationale="x",
        **kw,  # type: ignore[arg-type]
    )


class TestValidateRedditQuery:
    def test_well_formed_query_has_no_errors(self) -> None:
        spec = _spec(
            '(subreddit:startups OR subreddit:smallbusiness) AND "I would pay"'
        )
        assert validate_reddit_query(spec) == []

    def test_lowercase_or_is_flagged_skill_item_6(self) -> None:
        spec = _spec('(subreddit:a or subreddit:b)')
        errors = validate_reddit_query(spec)
        assert any("uppercase" in e.lower() for e in errors)

    def test_lowercase_and_is_flagged_skill_item_6(self) -> None:
        spec = _spec('subreddit:a and "phrase"')
        errors = validate_reddit_query(spec)
        assert any("uppercase" in e.lower() for e in errors)

    def test_invalid_subreddit_name_is_flagged_skill_item_10(self) -> None:
        # space inside name
        spec = _spec('subreddit:Small Business AND "x"')
        errors = validate_reddit_query(spec)
        assert any("subreddit" in e.lower() for e in errors)

    def test_hyphen_in_subreddit_name_is_flagged(self) -> None:
        spec = _spec('subreddit:my-sub AND "x"')
        errors = validate_reddit_query(spec)
        assert any("subreddit" in e.lower() for e in errors)

    def test_too_many_subreddits_in_site_wide_is_flagged_skill_item_7(self) -> None:
        subs = " OR ".join(f"subreddit:s{i}" for i in range(8))
        spec = _spec(f'({subs}) AND "x"')
        errors = validate_reddit_query(spec)
        assert any("subreddits" in e.lower() and "6" in e for e in errors)

    def test_per_sub_must_have_no_subreddit_clause_skill_item_16(self) -> None:
        """per_sub means the subreddit comes from the endpoint, not the q string."""
        spec = _spec("subreddit:a AND \"x\"", endpoint="per_sub")
        errors = validate_reddit_query(spec)
        assert any("per_sub" in e.lower() for e in errors)

    def test_site_wide_must_have_at_least_one_subreddit_clause(self) -> None:
        spec = _spec('"phrase only, no subreddit"', endpoint="site_wide")
        errors = validate_reddit_query(spec)
        assert any("site_wide" in e.lower() or "subreddit" in e.lower() for e in errors)

    def test_word_or_inside_a_quoted_phrase_is_not_flagged(self) -> None:
        """`"oranges"` contains the substring 'or' — we must not false-positive."""
        spec = _spec(
            '(subreddit:cooking OR subreddit:food) AND "oranges or apples"'
        )
        # The `or` inside the quoted phrase isn't an operator. Should be valid.
        errors = validate_reddit_query(spec)
        assert errors == [], f"unexpected errors: {errors}"
```

- [ ] **Step 2: Run the tests, confirm fails**

`uv run pytest tests/unit/test_orchestrator_reddit_query_validator.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement the validator**

Create `src/discovery/orchestrator/reddit_query_validator.py`:

```python
"""Validate LLM-built Reddit search queries against the source's rules.

Pure function. Returns a list of human-readable violation strings —
empty list means the query passes. The Wave 0 station drops any
`RedditQuerySpec` whose validator returns a non-empty list.

Each check is keyed to a numbered item in
`.claude/skills/reddit-source/SKILL.md` so reviewers can trace rule →
code → test.
"""

from __future__ import annotations

import re

from discovery.llm.schemas import RedditQuerySpec

# Skill item 10: subreddit names — 3 to 21 chars, ASCII letters/digits/underscore.
_VALID_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{3,21}$")
_MAX_SUBS_SITE_WIDE = 6  # skill item 7


def validate_reddit_query(spec: RedditQuerySpec) -> list[str]:
    """Return a list of skill-rule violations. Empty list = valid."""
    errors: list[str] = []

    q_stripped = _strip_quoted_substrings(spec.q)

    _check_uppercase_operators(q_stripped, errors)
    _check_subreddit_names(spec.q, errors)
    _check_endpoint_subreddit_count(spec, errors)

    return errors


def _strip_quoted_substrings(q: str) -> str:
    """Remove text inside double-quoted phrases so checks for `or` / `and`
    don't false-positive on words like "oranges" or "candy and chips"."""
    return re.sub(r'"[^"]*"', "", q)


def _check_uppercase_operators(q_stripped: str, errors: list[str]) -> None:
    """Skill item 6: OR / AND must be uppercase outside of quoted phrases."""
    # Word-boundaried lowercase or/and, not adjacent to an uppercase letter
    # (which would mean it's part of another word).
    if re.search(r"(?<![A-Za-z])(or|and)(?![A-Za-z])", q_stripped):
        errors.append(
            "Reddit operators OR/AND must be uppercase outside of quoted "
            "phrases (skill item 6)."
        )


def _check_subreddit_names(q: str, errors: list[str]) -> None:
    """Skill item 10: subreddit names must match [A-Za-z0-9_]{3,21}."""
    # subreddit: tokens — grab everything up to the next whitespace or paren
    for match in re.finditer(r"subreddit:(\S+?)(?=[\s\)]|$)", q):
        name = match.group(1)
        if not _VALID_SUBREDDIT.match(name):
            errors.append(
                f"Invalid subreddit name '{name}' (skill item 10: "
                f"3–21 chars, [A-Za-z0-9_])."
            )


def _check_endpoint_subreddit_count(
    spec: RedditQuerySpec, errors: list[str]
) -> None:
    """Skill items 7 (cap site_wide at ~6) and 16 (per_sub uses the endpoint)."""
    sub_count = len(re.findall(r"\bsubreddit:", spec.q))
    if spec.endpoint == "per_sub" and sub_count > 0:
        errors.append(
            "per_sub queries must not include a subreddit: clause — the "
            "subreddit comes from the endpoint (skill item 16)."
        )
    if spec.endpoint == "site_wide":
        if sub_count == 0:
            errors.append(
                "site_wide queries must include at least one subreddit: "
                "clause to avoid scanning all of Reddit (skill item 16)."
            )
        if sub_count > _MAX_SUBS_SITE_WIDE:
            errors.append(
                f"site_wide query has {sub_count} subreddits; cap is "
                f"{_MAX_SUBS_SITE_WIDE} (skill item 7)."
            )
```

- [ ] **Step 4: Run the tests, confirm pass**

`uv run pytest tests/unit/test_orchestrator_reddit_query_validator.py -v`
Expected: 9 PASS. If any of the boundary cases fail, the regexes need
tightening — the `or`-inside-quotes test is the trickiest.

- [ ] **Step 5: Lint + types**

```
uv run ruff check --fix src/discovery/orchestrator/reddit_query_validator.py
uv run mypy src/discovery/orchestrator/reddit_query_validator.py
```

- [ ] **Step 6: Commit**

```bash
git add src/discovery/orchestrator/reddit_query_validator.py tests/unit/test_orchestrator_reddit_query_validator.py
git commit -m "feat(orchestrator): add Reddit query validator (skill items 6/7/10/16)"
```

### Task 5.3: Build the `run_query_expansion` station

Cache + LLM call + validation + drop-invalid. If the post-validation
count drops below 10, raise a `QueryExpansionFailure` so the caller
(plan_job) can fall back. Cache miss path calls OpenAI exactly once.

**Files:**
- Create: `src/discovery/llm/stations/__init__.py` (empty)
- Create: `src/discovery/llm/stations/query_expansion.py`
- Test: `tests/unit/llm/stations/__init__.py` (empty), `tests/unit/llm/stations/test_query_expansion.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/llm/stations/__init__.py` (empty).

Create `tests/unit/llm/stations/test_query_expansion.py`:

```python
"""Tests for `discovery.llm.stations.query_expansion.run_query_expansion`.

We never call the real OpenAI here. Either:
  - the diskcache is pre-populated with a known JobPlan (cache-hit path)
  - the `call_openai` function is monkeypatched to return a stub
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from discovery.jobs import JobSpec
from discovery.llm import cache as cache_module
from discovery.llm.cache import cache_key, make_cache, put_cached
from discovery.llm.prompts import query_expansion as qe
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.llm.stations import query_expansion as station
from discovery.llm.stations.query_expansion import (
    MODEL,
    QueryExpansionFailure,
    run_query_expansion,
)


def _valid_query(label: str = "x") -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint="site_wide",
        q=f'(subreddit:startups OR subreddit:smallbusiness) AND "{label}"',
        rationale=label,
    )


def _valid_plan() -> JobPlan:
    return JobPlan(reddit_queries=[_valid_query(f"q{i}") for i in range(10)])


@pytest.fixture
def tmp_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the station's cache at a temp dir for each test."""
    cache = make_cache(tmp_path / "cache")
    monkeypatch.setattr(station, "_cache", cache)
    return cache


@pytest.fixture
def spec() -> JobSpec:
    return JobSpec(industry="commercial cleaning", as_of=date(2026, 6, 1))


class TestCacheHit:
    async def test_returns_cached_without_calling_llm(
        self,
        tmp_cache: Any,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plan = _valid_plan()
        key = cache_key(
            spec=spec.model_dump(mode="json"),
            prompt_version=qe.VERSION,
            model=MODEL,
        )
        put_cached(tmp_cache, key, plan)

        # Make the LLM call raise if it's actually hit:
        async def _explode(**kwargs: Any) -> None:
            raise AssertionError("LLM should not be called on cache hit")

        monkeypatch.setattr(station, "call_openai", _explode)

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)
        assert len(result.reddit_queries) == 10


class TestCacheMiss:
    async def test_calls_llm_and_caches_result(
        self,
        tmp_cache: Any,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            captured.update(kwargs)
            return _valid_plan()

        monkeypatch.setattr(station, "call_openai", _stub_llm)

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)
        assert captured["model"] == MODEL
        # second call now hits the cache; stub should not run again
        async def _explode(**kwargs: Any) -> None:
            raise AssertionError("expected cache hit on second call")

        monkeypatch.setattr(station, "call_openai", _explode)
        again = await run_query_expansion(spec)
        assert len(again.reddit_queries) == len(result.reddit_queries)


class TestValidationDropsInvalidQueries:
    async def test_drops_lowercase_or_query(
        self,
        tmp_cache: Any,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = [_valid_query(f"g{i}") for i in range(10)]
        bad = RedditQuerySpec(
            endpoint="site_wide",
            q="(subreddit:a or subreddit:b) AND \"x\"",  # lowercase or
            rationale="bad",
        )
        plan = JobPlan(reddit_queries=[*good, bad])

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return plan

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        result = await run_query_expansion(spec)
        assert len(result.reddit_queries) == 10  # bad one dropped


class TestFallbackOnTooFewValidQueries:
    async def test_raises_when_below_min_after_validation(
        self,
        tmp_cache: Any,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 10 valid + 5 invalid → JobPlan accepts (15 total), validator
        # drops 5 → 10 valid survive. Edge case: if 6 are invalid, only
        # 9 survive, which is below the floor and triggers failure.
        good = [_valid_query(f"g{i}") for i in range(9)]
        bad_template = RedditQuerySpec(
            endpoint="site_wide",
            q="(subreddit:a or subreddit:b) AND \"x\"",  # lowercase or
            rationale="bad",
        )
        plan = JobPlan(reddit_queries=[*good, *[bad_template] * 6])

        async def _stub_llm(**kwargs: Any) -> JobPlan:
            return plan

        monkeypatch.setattr(station, "call_openai", _stub_llm)
        with pytest.raises(QueryExpansionFailure):
            await run_query_expansion(spec)
```

- [ ] **Step 2: Run the tests, confirm fails**

`uv run pytest tests/unit/llm/stations/test_query_expansion.py -v`
Expected: ImportError on `discovery.llm.stations.query_expansion`.

- [ ] **Step 3: Implement the station**

Create `src/discovery/llm/stations/__init__.py` (empty).

Create `src/discovery/llm/stations/query_expansion.py`:

```python
"""Wave 0 — Query Expansion station.

Takes a `JobSpec`, returns a Pydantic-validated `JobPlan` with 10–15
Reddit search queries the LLM brainstormed for this industry.

Flow:
    1. Compute cache key over (spec, prompt VERSION, model).
    2. Cache hit? Return cached JobPlan.
    3. Cache miss? Call OpenAI with the query-expansion prompt.
    4. instructor enforces the JobPlan schema; bad JSON → exception.
    5. Run `validate_reddit_query` over each query; drop violators.
    6. If too few queries survive, raise `QueryExpansionFailure` —
       callers fall back to the deterministic template.
    7. Cache the validated, filtered plan.

Notes on temperature
--------------------
The skill-default for stations is `temperature=0`. We deviate slightly
(0.2) because the LLM is brainstorming creative query designs, not
classifying anything. Determinism here would just echo the few-shot
examples back. The skill's contract is updated in the same slice that
ships this station — see `.claude/skills/llm-station/SKILL.md`.
"""

from __future__ import annotations

from loguru import logger

from discovery.config.settings import settings
from discovery.jobs import JobSpec
from discovery.llm.cache import cache_key, get_cached, make_cache, put_cached
from discovery.llm.client import call_openai
from discovery.llm.prompts import query_expansion
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.orchestrator.reddit_query_validator import validate_reddit_query

MODEL: str = "gpt-5.4"
TEMPERATURE: float = 0.2
MIN_VALID_QUERIES: int = 10


class QueryExpansionFailure(Exception):
    """Raised when the station can't produce a valid JobPlan."""


_cache = make_cache(settings.llm_cache_dir)


async def run_query_expansion(spec: JobSpec) -> JobPlan:
    """Return a `JobPlan` for `spec`, brainstormed by gpt-5.4 and
    validated against the Reddit search rules.

    Raises `QueryExpansionFailure` if the LLM call fails or too few
    queries survive validation. The caller (`plan_job`) catches this
    and falls back to the deterministic template.
    """
    key = cache_key(
        spec=spec.model_dump(mode="json"),
        prompt_version=query_expansion.VERSION,
        model=MODEL,
    )
    cached = get_cached(_cache, key, JobPlan)
    if cached is not None:
        logger.debug("query_expansion cache hit for {}", key[:12])
        return cached

    logger.info("query_expansion cache miss; calling {}", MODEL)
    try:
        raw_plan = await call_openai(
            system=query_expansion.SYSTEM_PROMPT,
            user=query_expansion.build_user_message(spec),
            response_model=JobPlan,
            model=MODEL,
            temperature=TEMPERATURE,
        )
    except Exception as e:  # noqa: BLE001 — caller decides fallback
        raise QueryExpansionFailure(
            f"LLM call failed: {type(e).__name__}: {e}"
        ) from e

    filtered_plan = _drop_invalid_queries(raw_plan)
    if len(filtered_plan.reddit_queries) < MIN_VALID_QUERIES:
        raise QueryExpansionFailure(
            f"Only {len(filtered_plan.reddit_queries)} of "
            f"{len(raw_plan.reddit_queries)} queries passed validation; "
            f"need at least {MIN_VALID_QUERIES}."
        )

    put_cached(_cache, key, filtered_plan)
    return filtered_plan


def _drop_invalid_queries(plan: JobPlan) -> JobPlan:
    """Return a new JobPlan keeping only queries that pass `validate_reddit_query`."""
    kept: list[RedditQuerySpec] = []
    for q in plan.reddit_queries:
        errors = validate_reddit_query(q)
        if errors:
            logger.warning(
                "dropping invalid LLM query: errors={} q={!r}", errors, q.q
            )
            continue
        kept.append(q)
    # JobPlan requires min 10; if kept is shorter, model_validate raises —
    # let the caller decide. We construct via model_validate to preserve
    # extra fields (subreddits + future extras).
    data = plan.model_dump()
    data["reddit_queries"] = [k.model_dump() for k in kept]
    return JobPlan.model_validate(data) if len(kept) >= 10 else plan.model_copy(
        update={"reddit_queries": kept}, deep=True
    )
```

Note on the last function: if `kept` is shorter than 10, returning a
JobPlan would fail Pydantic's `min_length` check. We use `model_copy`
with `validate=False`-equivalent behavior (model_copy doesn't re-validate)
so the caller can read `len(plan.reddit_queries)` and raise.

Actually, `model_copy(update=..., deep=True)` does NOT re-validate, so
that works. Confirm by reading the Pydantic docs if you hit a runtime
error here — if it does re-validate in your version, do this instead:

```python
kept_plan = JobPlan.model_construct(
    reddit_queries=kept, reddit_subreddits=plan.reddit_subreddits
)
```

`model_construct` skips validation entirely.

- [ ] **Step 4: Run the tests, confirm pass**

`uv run pytest tests/unit/llm/stations/test_query_expansion.py -v`
Expected: 4 PASS.

If `_drop_invalid_queries` errors on the under-10 case, swap to
`model_construct` as noted above and re-run.

- [ ] **Step 5: Lint + types + full test sweep so far**

```
uv run ruff check --fix src/discovery/llm/ tests/unit/llm/
uv run mypy src/discovery/llm/
uv run pytest tests/unit/llm/ -v
```
Expected: all green. Final test count so far: 83 (existing) + ~25 new = ~108.

- [ ] **Step 6: Commit**

```bash
git add src/discovery/llm/stations/ tests/unit/llm/stations/
git commit -m "feat(llm): add Wave 0 Query Expansion station (gpt-5.4)"
```

---

## Chunk 6: Orchestration — `plan_job`, Reddit-orchestrator update, CLI wiring

The last bit: wire `run_query_expansion` into the CLI flow. Three
pieces:

1. `plan_job(session, job)` — orchestrator function that runs the
   station, writes `job.job_plan`, and on failure leaves `job.job_plan`
   null so Reddit falls back.
2. Modify `enqueue_reddit_task_for_job` to read from `job.job_plan`
   when present; fall back to the existing template when null.
3. Modify `cli/run.py` to call `plan_job` before
   `enqueue_reddit_task_for_job`.

### Task 6.1: Build `plan_job`

**Files:**
- Create: `src/discovery/orchestrator/jobs.py`
- Test: `tests/unit/test_orchestrator_jobs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_orchestrator_jobs.py`:

```python
"""Tests for `discovery.orchestrator.jobs.plan_job`.

We never call the real LLM — we monkeypatch `run_query_expansion`
inside the orchestrator module. plan_job's contract:

- On success: `job.job_plan` is populated with the JobPlan dict.
- On `QueryExpansionFailure`: `job.job_plan` stays null; the function
  returns the unchanged Job so the caller can proceed to fallback.
- Logs at info/warn levels — verify via caplog if needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 — registers tables
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.jobs import JobSpec, create_job
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.llm.stations.query_expansion import QueryExpansionFailure
from discovery.orchestrator import jobs as jobs_module
from discovery.orchestrator.jobs import plan_job


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()


def _valid_plan() -> JobPlan:
    return JobPlan(
        reddit_queries=[
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups OR subreddit:smallbusiness) AND "p{i}"',
                rationale="x",
            )
            for i in range(10)
        ]
    )


class TestPlanJob:
    async def test_populates_job_plan_on_success(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _stub(spec: JobSpec) -> JobPlan:
            return _valid_plan()

        monkeypatch.setattr(jobs_module, "run_query_expansion", _stub)
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        assert job.job_plan is None

        updated = await plan_job(session, job)
        assert updated.job_plan is not None
        assert "reddit_queries" in updated.job_plan
        assert len(updated.job_plan["reddit_queries"]) == 10

    async def test_leaves_job_plan_null_on_failure(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fail(spec: JobSpec) -> JobPlan:
            raise QueryExpansionFailure("simulated")

        monkeypatch.setattr(jobs_module, "run_query_expansion", _fail)
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        updated = await plan_job(session, job)
        assert updated.job_plan is None

    async def test_idempotent_returns_existing_plan(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If job.job_plan is already populated, don't call the LLM again."""
        async def _explode(spec: JobSpec) -> JobPlan:
            raise AssertionError("station should not be called when plan exists")

        monkeypatch.setattr(jobs_module, "run_query_expansion", _explode)
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        # manually set the plan to simulate a re-run
        job.job_plan = _valid_plan().model_dump()
        session.add(job)
        await session.commit()

        updated = await plan_job(session, job)
        assert updated.job_plan is not None
```

- [ ] **Step 2: Run the test, confirm fails**

`uv run pytest tests/unit/test_orchestrator_jobs.py -v`
Expected: ImportError on `discovery.orchestrator.jobs`.

- [ ] **Step 3: Implement `plan_job`**

Create `src/discovery/orchestrator/jobs.py`:

```python
"""Cross-source job-level orchestration.

`plan_job(session, job)` runs Wave 0 (LLM query expansion) inline and
populates `Job.job_plan`. On failure it leaves `job_plan` null and
returns the unchanged Job — the per-source orchestrators are
responsible for falling back to their deterministic templates when
`job_plan` is absent.

NOTE TO FUTURE SESSIONS
-----------------------
This is the "inline Option A" implementation. The task-based "Option B"
alternative was considered and deferred (see
`docs/plans/2026-05-14-wave-0-query-expansion.md`). Promotion to a
worker task is a ~20-line wrapper around `plan_job`; the LLM call
itself is already orchestrator-agnostic via `run_query_expansion`.
"""

from __future__ import annotations

from loguru import logger
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job
from discovery.jobs import JobSpec
from discovery.llm.stations.query_expansion import (
    QueryExpansionFailure,
    run_query_expansion,
)


async def plan_job(session: AsyncSession, job: Job) -> Job:
    """Run Wave 0 query expansion for `job`. Idempotent and fault-tolerant.

    - If `job.job_plan` is already populated, returns the job unchanged
      (re-running `discovery run` shouldn't burn a second LLM call).
    - On success: writes the JobPlan dict to `job.job_plan`, commits.
    - On `QueryExpansionFailure`: logs a warning, leaves `job.job_plan`
      null, returns the job unchanged. Per-source orchestrators must
      detect a null `job_plan` and fall back to their templates.

    Returns the (possibly updated) Job.
    """
    if job.job_plan is not None:
        logger.debug("plan_job: job {} already planned; skipping LLM call", job.id)
        return job

    spec = JobSpec.model_validate(job.spec)
    try:
        plan = await run_query_expansion(spec)
    except QueryExpansionFailure as e:
        logger.warning(
            "plan_job: query expansion failed for job {}: {}; "
            "Reddit orchestrator will use the deterministic template.",
            job.id,
            e,
        )
        return job

    job.job_plan = plan.model_dump()
    session.add(job)
    await session.commit()
    await session.refresh(job)
    logger.info(
        "plan_job: job {} planned with {} reddit_queries",
        job.id,
        len(plan.reddit_queries),
    )
    return job
```

- [ ] **Step 4: Run the tests, confirm pass**

`uv run pytest tests/unit/test_orchestrator_jobs.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/discovery/orchestrator/jobs.py tests/unit/test_orchestrator_jobs.py
git commit -m "feat(orchestrator): add plan_job (Wave 0 inline, fallback-safe)"
```

### Task 6.2: Modify Reddit orchestrator to read from `job_plan`

Update `reddit_queries_for_spec` callers and `enqueue_reddit_task_for_job`
to prefer `job.job_plan["reddit_queries"]` when present.

**Files:**
- Modify: `src/discovery/orchestrator/reddit.py`
- Modify: `tests/unit/test_orchestrator_reddit.py`

- [ ] **Step 1: Write a new test for the job_plan path**

Add to `tests/unit/test_orchestrator_reddit.py` (after the existing
tests), a new class:

```python
from discovery.llm.schemas import JobPlan, RedditQuerySpec


class TestReadsFromJobPlan:
    async def test_uses_job_plan_queries_when_present(
        self, session: AsyncSession
    ) -> None:
        """When job.job_plan is populated, the orchestrator uses those
        queries verbatim — does not fall through to the hand-rolled
        template."""
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        llm_queries = [
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups) AND "llm{i}"',
                rationale="x",
            )
            for i in range(10)
        ]
        job.job_plan = JobPlan(reddit_queries=llm_queries).model_dump()
        session.add(job)
        await session.commit()

        task = await enqueue_reddit_task_for_job(session, job)
        # The task's queries list contains the LLM strings, not the
        # template's.
        assert len(task.params["queries"]) == 10
        for q in task.params["queries"]:
            assert '"llm' in q["q"]

    async def test_falls_back_to_template_when_job_plan_null(
        self, session: AsyncSession
    ) -> None:
        """No job_plan → use the existing hand-rolled template."""
        job = await create_job(
            session, JobSpec(industry="cleaning", as_of=date(2026, 6, 1))
        )
        assert job.job_plan is None
        task = await enqueue_reddit_task_for_job(session, job)
        assert "queries" in task.params
        # Template produces 4 queries currently; LLM path produces 10+.
        # Pin a sentinel that's stable for the template path.
        assert all('"cleaning"' in q["q"] for q in task.params["queries"])
```

- [ ] **Step 2: Run the test, confirm fails**

`uv run pytest tests/unit/test_orchestrator_reddit.py::TestReadsFromJobPlan -v`
Expected: The first test fails because the orchestrator currently ignores
`job_plan` and always uses the template; the second passes incidentally.

- [ ] **Step 3: Modify the orchestrator**

In `src/discovery/orchestrator/reddit.py`, update
`enqueue_reddit_task_for_job` to check `job.job_plan` first:

```python
from discovery.llm.schemas import JobPlan, RedditQuerySpec


async def enqueue_reddit_task_for_job(session: AsyncSession, job: Job) -> Task:
    """Queue one Reddit fetch task for `job`. Idempotent on `content_hash`.

    Query source priority:

      1. `job.job_plan["reddit_queries"]` if populated (Wave 0 LLM output)
      2. `reddit_queries_for_spec(spec)` — the deterministic template

    See module docstring for fallback rules.
    """
    spec = JobSpec.model_validate(job.spec)
    queries = _queries_from_job_plan(job) or reddit_queries_for_spec(spec)
    params: dict[str, Any] = {"queries": queries}
    content_hash = hash_params({"source": "reddit", "action": "fetch", "params": params})

    existing = await session.exec(
        select(Task).where(
            Task.job_id == job.id,
            Task.content_hash == content_hash,
        )
    )
    task = existing.first()
    if task is not None:
        return task

    task = Task(
        job_id=job.id,
        wave=1,
        source="reddit",
        action="fetch",
        params=params,
        content_hash=content_hash,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


def _queries_from_job_plan(job: Job) -> list[dict[str, Any]] | None:
    """Extract Reddit queries from a populated `job_plan`, or None.

    Returns None when `job_plan` is null (Wave 0 hasn't run or failed)
    OR when validation of the stored dict fails for any reason. Caller
    falls back to the deterministic template in either case.
    """
    if job.job_plan is None:
        return None
    try:
        plan = JobPlan.model_validate(job.job_plan)
    except Exception:  # noqa: BLE001 — corrupted plan → fall back
        return None
    return [_compile_query(q) for q in plan.reddit_queries]


def _compile_query(spec: RedditQuerySpec) -> dict[str, Any]:
    """Compile a `RedditQuerySpec` into the dict shape `RedditSource.fetch`
    accepts.

    The LLM has already filled in `q`; validation has already dropped
    bad ones; this is just shape conversion.
    """
    return {
        "endpoint": spec.endpoint,
        "q": spec.q,
        "sort": spec.sort,
        "t": spec.t,
        "limit": spec.limit,
    }
```

Update the module docstring too — note the new priority order and the
fallback rule.

- [ ] **Step 4: Run the new + existing tests, confirm all pass**

`uv run pytest tests/unit/test_orchestrator_reddit.py -v`
Expected: all existing tests still pass (template path unchanged) +
the two new ones pass.

- [ ] **Step 5: Lint + types**

```
uv run ruff check --fix src/discovery/orchestrator/reddit.py tests/unit/test_orchestrator_reddit.py
uv run mypy src/discovery/orchestrator/reddit.py
```

- [ ] **Step 6: Commit**

```bash
git add src/discovery/orchestrator/reddit.py tests/unit/test_orchestrator_reddit.py
git commit -m "feat(orchestrator): Reddit reads from job.job_plan with template fallback"
```

### Task 6.3: Wire `plan_job` into the CLI

Insert one line into `cli/run.py` between `create_job` and the existing
enqueue call.

**Files:**
- Modify: `src/discovery/cli/run.py`
- (No new tests — covered by Task 6.1 and 6.2; an end-to-end smoke test
  would require network access to OpenAI and is deferred.)

- [ ] **Step 1: Read `cli/run.py` to find the exact insertion point**

Find the line `task = await enqueue_reddit_task_for_job(session, job)`
(or similar). Insert `job = await plan_job(session, job)` directly
before it. Add the import at the top.

- [ ] **Step 2: Update the CLI**

```python
from discovery.orchestrator.jobs import plan_job

# ... inside the run command's async body, after create_job ...

job = await plan_job(session, job)
# plan_job returns the same job; on success job.job_plan is populated,
# on failure it stays null and the Reddit orchestrator falls back.

task = await enqueue_reddit_task_for_job(session, job)
```

- [ ] **Step 3: Run all tests + lint + types**

```
uv run pytest -v
uv run ruff check --fix src/ tests/
uv run mypy src/
```
Expected: all green.

- [ ] **Step 4: Smoke test (no real LLM)**

Run the CLI against an unset `OPENAI_API_KEY` so the fallback path
exercises end-to-end:

```bash
# In one shell:
unset OPENAI_API_KEY  # or remove from .env temporarily
uv run python -m discovery.cli.init_db
uv run discovery run --industry "cleaning" --location NY
```

Expected output:
- Job created.
- Log line: `plan_job: query expansion failed ... template will be used.`
- Reddit task enqueued with 4 template queries.
- Worker drains the task; `raw_records` row count > 0.

This proves the fallback chain works end-to-end without an OpenAI key.

- [ ] **Step 5: Commit**

```bash
git add src/discovery/cli/run.py
git commit -m "feat(cli): call plan_job between create_job and enqueue"
```

---

## Chunk 7: Docs + skill addendum + final verify

The last mile: update the llm-station skill to clarify per-station
provider/model/temperature choices, update the handoff log to mark
Wave 0 done and surface the future-B-revisit note, and a final
`/run-checks` sweep.

### Task 7.1: Update `llm-station/SKILL.md`

The skill currently says "default model is Sonnet" and
"temperature=0". Add a brief addendum about deviation rules.

**Files:**
- Modify: `.claude/skills/llm-station/SKILL.md`

- [ ] **Step 1: Append the addendum**

Add the following section at the end of the skill file, before the
existing "Cost knob" section if you want it grouped, or after if you
prefer chronological:

```markdown
## Per-station provider and tuning

The default model is Anthropic Sonnet, default temperature is 0. Stations
may deviate when there's a clear reason, documented in the station file
and called out here:

| Station          | Provider  | Model     | Temperature | Why                                           |
|------------------|-----------|-----------|-------------|-----------------------------------------------|
| Query Expansion  | OpenAI    | gpt-5.4   | 0.2         | Brainstorming creative queries; 0 just echoes few-shot |
| (others)         | Anthropic | sonnet-4-5| 0           | Default                                       |

Rules:

- Bump the prompt VERSION when changing model OR temperature — the
  cache key includes both indirectly via the prompt module's behavior.
- The `call_<provider>` function you import determines the provider.
  No generic `call_llm` dispatcher exists; pick a side.
- A station's deviation must be a deliberate decision, not a typo. New
  stations should justify any deviation from `(anthropic, sonnet, 0)`
  in their module docstring.
```

- [ ] **Step 2: Run `mypy` to make sure nothing else broke**

(The skill update isn't code, but a paranoid full sweep is cheap.)

```
uv run mypy src/
uv run pytest -v
```

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/llm-station/SKILL.md
git commit -m "docs(skills): clarify per-station provider/model/temperature rules"
```

### Task 7.2: Update `docs/handoff.md`

Mark Wave 0 done. Note the future-B-revisit option for the next session.

**Files:**
- Modify: `docs/handoff.md`

- [ ] **Step 1: Update the "What runs end-to-end today" section**

Add a line:
> Wave 0 (LLM query expansion via OpenAI gpt-5.4) runs inline before
> Wave 1 enqueue. Falls back to the hand-rolled template on LLM failure
> or missing API key. JobPlan is cached on `Job.job_plan`.

- [ ] **Step 2: Move Wave 0 out of "What's NOT built yet"**

Delete the "Wave 0" bullet from the NOT-built list. Update the test
count line ("83 unit tests" → whatever the current count is, likely
~110–125).

- [ ] **Step 3: Add a "Future considerations" section**

Append (near the bottom, before the GPT-5.4 sources list):

```markdown
## Future considerations — promoting Wave 0 to Option B

The Wave 0 LLM call currently runs inline in `plan_job(session, job)`.
The architecture rule "LLM calls are tasks, not function calls" was
deliberately deferred for this station — see
`docs/plans/2026-05-14-wave-0-query-expansion.md`'s "Decision record"
section. Promote to a worker task when:

- A second worker process is introduced (parallel job runs would
  benefit from queue-level concurrency).
- A `discovery status` dashboard wants Wave 0 failures visible in
  `tasks` alongside other failures.
- Cumulative serial-orchestration overhead starts showing up in
  measurements (re-measure first; A's overhead is ~50 ms per job).

The promotion path is a ~20-line `wave_0_task` wrapper around
`plan_job`. `run_query_expansion(spec) -> JobPlan` is already
orchestrator-agnostic; no station code changes.
```

- [ ] **Step 4: Update "Next slice"**

Replace the Wave 0 section under "Next slice" with the next-up wave —
likely Wave 2 (pain classification) or a second source adapter. If
unsure, write a placeholder and let the user direct it next session.

- [ ] **Step 5: Commit**

```bash
git add docs/handoff.md
git commit -m "docs: mark Wave 0 done; document future-B-revisit"
```

### Task 7.3: Final `/run-checks` + push

- [ ] **Step 1: Run the full quality gate**

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest -v
```

Expected: all green. Test count around 110–125 (83 existing + ~30 new).

- [ ] **Step 2: Update `docs/handoff.md` with the new commit list**

The handoff's "Commit history" table at the top is stale after this
slice. Update it to include the new commits from Chunks 1–7. (You can
take the SHAs from `git log --oneline -n 20`.)

- [ ] **Step 3: Final commit if the handoff needs another touchup**

```bash
git add docs/handoff.md
git commit -m "docs: refresh handoff commit list after Wave 0"
```

- [ ] **Step 4: Push**

```bash
git push -u origin <branch-name>
```

- [ ] **Step 5: Tell the user it's done**

Report:
- Tests added: ~30
- New modules: 7 (cache, schemas, prompts/query_expansion,
  stations/query_expansion, orchestrator/jobs, orchestrator/reddit_query_validator)
- Modified modules: client.py, settings.py, orchestrator/reddit.py, cli/run.py
- Skill updated: llm-station
- Handoff updated.
- Wave 0 runs end-to-end with fallback verified.

---

## How to verify when you finish

```bash
$ uv sync                              # install (incl. openai)
$ uv run pytest                        # expect: ~110–125 passed
$ uv run ruff check .                  # expect: All checks passed!
$ uv run ruff format --check .         # expect: all files formatted
$ uv run mypy src/                     # expect: Success
$ uv run discovery run --industry "commercial cleaning" --location NY
   # expect: Job created → Wave 0 logs (cache miss / hit) → Reddit task
   # enqueued with 10–15 LLM queries OR 4 template queries on fallback
```

If `OPENAI_API_KEY` is set in your `.env`, the first run hits the LLM
and caches the result; the second run is free.

If `OPENAI_API_KEY` is absent, the fallback log line fires and the
template's 4 queries are used. Wave 1 still works.

---

## Notes for the executor

- Every commit message uses Conventional Commits (`feat:`, `chore:`,
  `docs:`, `refactor:`).
- Don't skip the lint/type checks between tasks. Catching a typo
  early is cheaper than backing out three tasks later.
- If a Pydantic version mismatch makes `model_construct` or
  `model_copy` behave unexpectedly, check the installed version
  (`uv run python -c "import pydantic; print(pydantic.VERSION)"`)
  and adapt — the project requires `pydantic>=2.7`.
- If `instructor.from_openai` errors with a structured-output mode
  message, the project's `instructor>=1.4` should be new enough; try
  `instructor.from_provider("openai/gpt-5.4")` as an alternative
  entry point (some instructor versions prefer that idiom).
- The OpenAI rate limit for gpt-5.4 is on your account, not the SDK.
  If you see real 429s in production, tenacity handles them via the
  retry decorator — but watch the test for `test_raises_when_api_key_missing`
  to make sure it doesn't accidentally surface a 401.
- The `_drop_invalid_queries` function may need `model_construct`
  instead of `model_copy` depending on Pydantic version behavior — see
  Task 5.3 Step 3 for the swap.

---
