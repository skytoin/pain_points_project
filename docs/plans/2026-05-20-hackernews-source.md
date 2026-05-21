# HackerNews Source Adapter Implementation Plan

> **For agentic workers:** REQUIRED: Use @superpowers:subagent-driven-development to implement this plan (fresh subagent per task + two-stage review). Steps use checkbox (`- [ ]`) syntax for tracking. The authoritative design is [`docs/specs/2026-05-20-hackernews-source-design.md`](../specs/2026-05-20-hackernews-source-design.md) — every locked decision in this plan traces back to a section there.

**Goal:** Add HackerNews (Algolia API) as the second Wave-1 source so every `discovery run` fans out to Reddit AND HN concurrently and stores HN's Algolia hits verbatim into the existing Bronze layer.

**Architecture:** Approach A — the existing Wave-0 LLM emits HN keyword candidates (`HackerNewsKeywordSpec`) alongside Reddit's outputs in one combined v6 prompt call; Python in a new `orchestrator/hackernews.py` owns all mechanical concerns (token decomposition via `keyword_tokens.decompose_keyword`, deterministic 2:1 endpoint/tag routing from the LLM's `intent` flag, server-side `numericFilters` from `JobSpec.time_window`, ≤6 query cap). The locked Wave-0 deterministic tail stays byte-for-byte Reddit-only via a single carry-through helper in `run_query_expansion`. Parallel HN+Reddit dispatch is achieved in `cli/run.py` via `asyncio.gather` calling a new additive `claim_known_task` worker primitive that routes around the single-worker-safe `claim_one`. HN sparsity on non-tech industries is graceful — empty/thin `hn_queries` falls back to a deterministic capability-first template.

**Tech Stack:** Python 3.12, async httpx + aiolimiter (per-instance for HN), Pydantic 2 (frozen + `default_factory=list` permissive), SQLModel/SQLAlchemy async, `instructor` + OpenAI gpt-5.4 (existing Wave-0 station, prompt VERSION v5 → v6), pytest with `httpx.MockTransport`, loguru.

---

## Pre-flight (verify before any code)

- [ ] **Read the spec end-to-end:** [`docs/specs/2026-05-20-hackernews-source-design.md`](../specs/2026-05-20-hackernews-source-design.md). Locked decisions sit in §3, §6 (carry-through), §11 (no retry), §12 (parallel fan-out), §16 (divergences), §17 (invariants).
- [ ] **Read these project skills:** @source-adapter (umbrella contract), @reddit-source (reference for mirroring patterns and what *not* to copy for HN), @llm-station (LLM call contract).
- [ ] **Verify project health before touching code:**

  ```
  uv run pytest
  uv run ruff check src tests
  uv run ruff format --check src tests
  uv run mypy src/
  ```

  All four must be green. If any are red, stop and fix before starting Task 1.1 — the plan assumes a clean baseline.

- [ ] **Environment note:** Run everything in WSL at `/mnt/c/Users/skyto/pain_points_poject`. uv works there. Worktrees on this checkout are unusable (a POSIX `lib64` reparse point breaks `uv` operations on the worktree's `.venv`), so execute the plan on the current branch in the main checkout — *not* in `.claude/worktrees/`. File reads/writes/commits via this session worked, but build/test must use the main checkout.

- [ ] **Commit-message format (every task):** subject in conventional-commit style (`feat(scope): ...`, `test(scope): ...`, `docs(scope): ...`), then a blank line, then the trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`. Use repeated `-m` flags or `git commit -F <file>`; do not omit the trailer.

- [ ] **Idempotency trap awareness:** re-running the same `industry+location+as_of+time_window` returns the cached old job and `plan_job` short-circuits. To re-test the v6 prompt against real LLM, change the `--industry` or `--as-of`. Wave-0 plans cache on `(spec, prompt_version, model)` — bumping `query_expansion.VERSION` v5 → v6 (Chunk 4) invalidates the combined cache automatically.

---

## Chunk 1: Pure foundations — schema + keyword decomposition

Three small, isolated additions with pure-function tests. No HTTP, no DB, no LLM. Each lands as its own atomic commit. Use @superpowers:test-driven-development for every task.

### Task 1.1: `HackerNewsKeywordSpec` schema

**Files:**
- Modify: `src/discovery/llm/schemas.py` — append `HackerNewsKeywordSpec` after `RedditQuerySpec`.
- Test: `tests/unit/llm/test_schemas.py` — add `TestHackerNewsKeywordSpec`. Create the file if it doesn't exist; otherwise add only the new class and the new import.

**Spec reference:** §7.

- [ ] **Step 1: Write the failing tests**

`tests/unit/llm/test_schemas.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from discovery.llm.schemas import HackerNewsKeywordSpec


class TestHackerNewsKeywordSpec:
    def test_minimal_valid(self) -> None:
        spec = HackerNewsKeywordSpec(
            keyword="local-first CRM",
            intent="launch",
            rationale="Show HN local-first CRM launches",
        )
        assert spec.keyword == "local-first CRM"
        assert spec.intent == "launch"
        assert spec.rationale == "Show HN local-first CRM launches"

    def test_intent_must_be_launch_or_context(self) -> None:
        with pytest.raises(ValidationError):
            HackerNewsKeywordSpec(keyword="x", intent="other", rationale="r")  # type: ignore[arg-type]

    def test_keyword_min_length(self) -> None:
        with pytest.raises(ValidationError):
            HackerNewsKeywordSpec(keyword="", intent="launch", rationale="r")

    def test_rationale_min_length(self) -> None:
        with pytest.raises(ValidationError):
            HackerNewsKeywordSpec(keyword="x", intent="launch", rationale="")

    def test_frozen_blocks_assignment(self) -> None:
        spec = HackerNewsKeywordSpec(keyword="x", intent="launch", rationale="r")
        with pytest.raises(ValidationError):
            spec.keyword = "y"  # type: ignore[misc]
```

If the file already exists, append only the class (and add `HackerNewsKeywordSpec` to the existing import line from `discovery.llm.schemas`). Do NOT duplicate `import pytest` or `from pydantic import ValidationError` if they're already at the top.

- [ ] **Step 2: Run the tests; expect failure**

```
uv run pytest tests/unit/llm/test_schemas.py::TestHackerNewsKeywordSpec -v
```

Expected: `ImportError: cannot import name 'HackerNewsKeywordSpec' from 'discovery.llm.schemas'` (the symbol doesn't exist yet).

- [ ] **Step 3: Implement `HackerNewsKeywordSpec`**

In `src/discovery/llm/schemas.py`, append after the `RedditQuerySpec` class (and before `JobPlan`):

```python
class HackerNewsKeywordSpec(BaseModel):
    """Wave 0 LLM HN keyword candidate. Python downstream decomposes,
    routes by intent, and compiles to an Algolia URL. See
    `docs/specs/2026-05-20-hackernews-source-design.md` §7.
    """

    model_config = ConfigDict(frozen=True)

    keyword: str = Field(
        min_length=1,
        max_length=80,
        description=(
            "Raw HN-suitable phrase, 2-4 words, casing preserved. "
            "Python keeps the first 2 surviving content tokens after "
            "stopword stripping; long phrases lose their tail tokens."
        ),
    )
    intent: Literal["launch", "context"] = Field(
        description=(
            "launch -> fired against /search_by_date with tags=show_hn "
            "and a relaxed quality floor (recency is the signal). "
            "context -> fired against /search with tags=story and the "
            "standard points/num_comments floor."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this HN candidate is worth running.",
    )
```

`Literal` is already imported at the top of `schemas.py` (used by `RedditQuerySpec.endpoint`). Verify the import line reads `from typing import Literal, Self` — it should; no change needed.

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/llm/test_schemas.py::TestHackerNewsKeywordSpec -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green. The 5 new tests pass; the existing test count rises by 5; ruff + mypy stay clean.

- [ ] **Step 5: Commit**

```
git add src/discovery/llm/schemas.py tests/unit/llm/test_schemas.py
git commit -m "feat(llm): add HackerNewsKeywordSpec schema (Wave 0 HN candidate)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.2: Add `JobPlan.hn_queries` typed field

**Files:**
- Modify: `src/discovery/llm/schemas.py` — add one field on `JobPlan`.
- Test: `tests/unit/llm/test_schemas.py` — add `TestJobPlanHnQueries`.

**Spec reference:** §7 ("Permissive default (no `min_length`) is deliberate"). The permissive default is load-bearing — a strict `min_length` on `hn_queries` would let HN under-production raise `QueryExpansionError` and sink the Reddit grounded plan.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/llm/test_schemas.py`:

```python
from discovery.llm.schemas import HackerNewsKeywordSpec, JobPlan, RedditQuerySpec  # ensure these are in the imports (HackerNewsKeywordSpec was added in Task 1.1)


def _make_reddit_queries(n: int = 25) -> list[RedditQuerySpec]:
    """Build N valid RedditQuerySpec to satisfy JobPlan's 25-30 band."""
    return [
        RedditQuerySpec(
            endpoint="site_wide",
            q=f'(subreddit:startups) AND "test{i}"',
            sort="top",
            t="month",
            limit=100,
            rationale="test",
        )
        for i in range(n)
    ]


class TestJobPlanHnQueries:
    def test_hn_queries_defaults_to_empty_list(self) -> None:
        plan = JobPlan(reddit_queries=_make_reddit_queries())
        assert plan.hn_queries == []

    def test_hn_queries_accepts_list(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(keyword="CRM CLI", intent="launch", rationale="r"),
            ],
        )
        assert len(plan.hn_queries) == 1
        assert plan.hn_queries[0].keyword == "CRM CLI"

    def test_hn_queries_round_trips_through_model_dump(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(keyword="x", intent="context", rationale="r"),
            ],
        )
        restored = JobPlan.model_validate(plan.model_dump())
        assert len(restored.hn_queries) == 1
        assert restored.hn_queries[0].intent == "context"

    def test_empty_hn_queries_does_not_break_validation(self) -> None:
        """A JobPlan with empty hn_queries must validate cleanly — the
        permissive default is what keeps HN sparsity from sinking the
        Reddit plan."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), hn_queries=[])
        assert plan.hn_queries == []
```

If you placed the `_make_reddit_queries` helper in a different file already, reuse it instead of duplicating.

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/llm/test_schemas.py::TestJobPlanHnQueries -v
```

Expected: `AttributeError: 'JobPlan' object has no attribute 'hn_queries'` on the first test.

- [ ] **Step 3: Implement the field**

In `src/discovery/llm/schemas.py`, add the `hn_queries` field to `JobPlan` after `reddit_subreddits`:

```python
class JobPlan(BaseModel):
    """LLM-produced query plan for one Job. Wave 0's output.

    See module docstring for why `extra="allow"` and how future
    sessions should extend it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    reddit_queries: list[RedditQuerySpec] = Field(min_length=25, max_length=30)
    reddit_subreddits: list[str] = Field(default_factory=list)
    hn_queries: list[HackerNewsKeywordSpec] = Field(
        default_factory=list,
        description=(
            "Wave 0 HN keyword candidates. Permissive default (no "
            "min_length) is deliberate: a strict floor would let HN "
            "under-production raise QueryExpansionError and sink the "
            "Reddit grounded plan. HN sparsity must degrade gracefully "
            "to the no-LLM template in orchestrator/hackernews.py."
        ),
    )
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/llm/test_schemas.py::TestJobPlanHnQueries -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green.

- [ ] **Step 5: Commit**

```
git add src/discovery/llm/schemas.py tests/unit/llm/test_schemas.py
git commit -m "feat(llm): add JobPlan.hn_queries typed field (permissive default)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.3: `keyword_tokens.decompose_keyword` pure helper

**Files:**
- Create: `src/discovery/sources/keyword_tokens.py`
- Test: Create `tests/unit/sources/test_keyword_tokens.py`

**Spec reference:** §9. Pure function; no I/O.

- [ ] **Step 1: Write the failing tests**

`tests/unit/sources/test_keyword_tokens.py`:

```python
from __future__ import annotations

from discovery.sources.keyword_tokens import MAX_TOKENS, decompose_keyword


class TestDecomposeKeyword:
    def test_two_tokens_pass_through(self) -> None:
        assert decompose_keyword("Personal CRM") == ["Personal", "CRM"]

    def test_drops_stopwords_case_insensitively(self) -> None:
        # `for` is a stopword regardless of the surrounding casing.
        assert decompose_keyword("X for Y") == ["X", "Y"]
        # Capitalised stopword also drops.
        assert decompose_keyword("The CRM workflow") == ["CRM", "workflow"]

    def test_preserves_casing_of_survivors(self) -> None:
        # HN acronyms must survive intact — MCP, CLI, RAG, LLM matter.
        assert decompose_keyword("MCP server") == ["MCP", "server"]
        assert decompose_keyword("CLI scheduling") == ["CLI", "scheduling"]
        assert decompose_keyword("billing CRDT") == ["billing", "CRDT"]

    def test_keeps_first_two_surviving_tokens_only(self) -> None:
        # Distinctive token in position 3 is silently dropped — the
        # very failure mode the §8 prompt warns against.
        assert decompose_keyword("vector database Rust") == ["vector", "database"]
        assert decompose_keyword("privacy preserving data collection library") == [
            "privacy",
            "preserving",
        ]

    def test_stopwords_do_not_count_against_cap(self) -> None:
        # "in" is a stopword; "Rust" survives because "in" was filtered first.
        assert decompose_keyword("MCP in Rust") == ["MCP", "Rust"]

    def test_empty_input(self) -> None:
        assert decompose_keyword("") == []

    def test_all_stopwords(self) -> None:
        assert decompose_keyword("the a an for") == []

    def test_whitespace_only_input(self) -> None:
        assert decompose_keyword("   \t  ") == []

    def test_extra_whitespace_collapses(self) -> None:
        # str.split() with no argument collapses any run of whitespace.
        assert decompose_keyword("  CLI   scheduling  ") == ["CLI", "scheduling"]

    def test_max_tokens_constant_is_two(self) -> None:
        assert MAX_TOKENS == 2
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/sources/test_keyword_tokens.py -v
```

Expected: `ModuleNotFoundError: No module named 'discovery.sources.keyword_tokens'`.

- [ ] **Step 3: Implement the module**

Create `src/discovery/sources/keyword_tokens.py`:

```python
"""Token decomposition for token-AND search APIs (HN Algolia).

Splits a raw keyword phrase into the small set of high-signal content
tokens HN's strict token-AND search will accept. Long phrases starve
the source, so we keep only the first 2 surviving tokens after a
small stopword strip, with original casing preserved (acronyms like
MCP, CLI, RAG, LLM matter on HN).

Reusable later by other token-AND backends (GitHub code search, arXiv,
etc.) -- kept here in the HN-adopting slice without pre-generalization
for unbuilt sources.
"""

from __future__ import annotations

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the",
        "for", "with", "to", "of", "in", "on",
        "and", "or",
    }
)

MAX_TOKENS: int = 2


def decompose_keyword(keyword: str) -> list[str]:
    """Return up to 2 content tokens from a raw HN keyword phrase.

    - Whitespace-split (no punctuation surgery -- HN's tokenizer is
      simple; we feed it as-is once stopwords are gone).
    - Filter tokens whose LOWERCASED form is in the stopword set
      (so the comparison is case-insensitive but surviving tokens
      retain their ORIGINAL casing).
    - Keep the first MAX_TOKENS surviving tokens.
    - Return [] if nothing survives (caller drops the query).
    """
    out: list[str] = []
    for tok in keyword.split():
        if tok.lower() in _STOPWORDS:
            continue
        out.append(tok)
        if len(out) == MAX_TOKENS:
            break
    return out
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/sources/test_keyword_tokens.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; the 10 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/sources/keyword_tokens.py tests/unit/sources/test_keyword_tokens.py
git commit -m "feat(sources): keyword_tokens.decompose_keyword for token-AND APIs" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

**End of Chunk 1.** After all three tasks are committed and `/run-checks` is green, the foundations are in place: a typed schema field, a typed candidate model, and a deterministic token decomposer. Nothing depends on them yet — they're standalone — but every subsequent chunk references at least one of them.

---

## Chunk 2: HN source adapter

Three tasks, each extending `src/discovery/sources/hackernews.py`. Pure helpers first, the adapter class last. The adapter has **no retry** — locked decision per spec §11 / §16 — and a per-instance `AsyncLimiter` (not a singleton) per spec §11. Tests use `httpx.MockTransport`, mirroring `tests/unit/sources/test_reddit.py` exactly in structure. Use @superpowers:test-driven-development for every task.

### Task 2.1: `build_search_url` + module skeleton

**Files:**
- Create: `src/discovery/sources/hackernews.py` (initial — imports + module docstring + `build_search_url`).
- Create: `tests/unit/sources/test_hackernews.py` (initial — imports + `TestBuildSearchUrl`).

**Spec reference:** §4 (Algolia API facts), §10 (compiled dict shape), §11 (URL builder helper).

- [ ] **Step 1: Write the failing tests**

`tests/unit/sources/test_hackernews.py`:

```python
from __future__ import annotations

from typing import Any

import pytest

from discovery.sources.hackernews import build_search_url


def _query(
    endpoint: str = "search",
    query: str = "Personal CRM",
    tags: str = "story",
    numeric_filters: str = "created_at_i>1700000000,points>5,num_comments>3",
    hits_per_page: int = 30,
) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "query": query,
        "tags": tags,
        "numeric_filters": numeric_filters,
        "hits_per_page": hits_per_page,
    }


class TestBuildSearchUrl:
    def test_routes_search_endpoint(self) -> None:
        url = build_search_url(_query(endpoint="search"))
        assert url.startswith("https://hn.algolia.com/api/v1/search?")

    def test_routes_search_by_date_endpoint(self) -> None:
        url = build_search_url(
            _query(endpoint="search_by_date", tags="show_hn", numeric_filters="created_at_i>1")
        )
        assert url.startswith("https://hn.algolia.com/api/v1/search_by_date?")

    def test_serializes_tags(self) -> None:
        url = build_search_url(_query(tags="story"))
        assert "tags=story" in url

    def test_serializes_numeric_filters_as_camelcase(self) -> None:
        # snake_case key in the compiled dict; Algolia expects camelCase in the URL.
        url = build_search_url(_query(numeric_filters="points>5,num_comments>3"))
        assert "numericFilters=" in url
        assert "numeric_filters" not in url

    def test_hits_per_page_passes_through(self) -> None:
        url = build_search_url(_query(hits_per_page=30))
        assert "hitsPerPage=30" in url

    def test_no_pagination_parameter(self) -> None:
        url = build_search_url(_query())
        # We never paginate -- top 30 by relevance/date is plenty (skill).
        assert "page=" not in url

    def test_query_url_encoded(self) -> None:
        url = build_search_url(_query(query="Personal CRM"))
        # urlencode encodes space as `+`.
        assert "query=Personal+CRM" in url

    def test_numeric_filters_url_encoded(self) -> None:
        # `>` becomes %3E in URL encoding.
        url = build_search_url(_query(numeric_filters="created_at_i>1700000000"))
        assert "created_at_i%3E1700000000" in url

    def test_unknown_endpoint_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown HN endpoint"):
            build_search_url(_query(endpoint="search_by_relevance"))
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/sources/test_hackernews.py -v
```

Expected: `ModuleNotFoundError: No module named 'discovery.sources.hackernews'`.

- [ ] **Step 3: Implement the module skeleton + `build_search_url`**

Create `src/discovery/sources/hackernews.py`:

```python
"""HackerNews source adapter via the Algolia HN Search API.

See `.claude/skills/source-adapter/SKILL.md` for the umbrella contract
and `docs/specs/2026-05-20-hackernews-source-design.md` for the HN-
specific design. Once the `hackernews-source` project skill lands in
Chunk 5, it becomes the operational reference for this file.

This module grows in three tasks (Chunk 2):

1. `build_search_url` -- pure URL builder for both Algolia endpoints.
2. `keep_hit`, `hit_to_raw_record` -- pure hit conversion helpers.
3. `HackerNewsSource(BaseSource)` -- the adapter class.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"


def build_search_url(query: dict[str, Any]) -> str:
    """Build an Algolia HN Search URL from a compiled query spec.

    Required keys in `query`:

    - `endpoint`        -- `"search"` or `"search_by_date"`
    - `query`           -- full-text search string (already
      decomposed to <=2 content tokens by the orchestrator)
    - `tags`            -- Algolia tag filter (`"story"` or `"show_hn"`)
    - `numeric_filters` -- comma-AND filter string (e.g.
      `"created_at_i>1715040000,points>5,num_comments>3"`)
    - `hits_per_page`   -- int; the orchestrator sets this to 30 (no-
      pagination policy, spec §11)
    """
    endpoint = query["endpoint"]
    if endpoint not in ("search", "search_by_date"):
        raise ValueError(f"unknown HN endpoint: {endpoint!r}")
    params = {
        "query": query["query"],
        "tags": query["tags"],
        "numericFilters": query["numeric_filters"],
        "hitsPerPage": str(query["hits_per_page"]),
    }
    return f"{_ALGOLIA_BASE}/{endpoint}?{urlencode(params)}"
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/sources/test_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; the 9 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/sources/hackernews.py tests/unit/sources/test_hackernews.py
git commit -m "feat(sources): hackernews module skeleton + build_search_url" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2.2: `keep_hit` + `hit_to_raw_record` pure helpers

**Files:**
- Modify: `src/discovery/sources/hackernews.py` — add two pure helpers + the `RawRecord` import.
- Modify: `tests/unit/sources/test_hackernews.py` — add `TestKeepHit` + `TestHitToRawRecord`.

**Spec reference:** §11 (`keep_hit` near-noop; `hit_to_raw_record` verbatim).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/sources/test_hackernews.py`. Extend the existing import line for `discovery.sources.hackernews` to include `hit_to_raw_record, keep_hit`, and add a new import for `RawRecord`:

```python
from discovery.sources.base import RawRecord  # add at top with other imports
from discovery.sources.hackernews import build_search_url, hit_to_raw_record, keep_hit  # extend


class TestKeepHit:
    def test_keeps_normal_hit(self) -> None:
        assert keep_hit({"objectID": "12345", "title": "x"})

    def test_drops_hit_without_object_id(self) -> None:
        """Defensive — Algolia always returns objectID, but if a hit
        ever lacked it we couldn't dedupe and Bronze would break."""
        assert not keep_hit({"title": "x"})


class TestHitToRawRecord:
    def test_external_id_is_object_id_string(self) -> None:
        hit = {
            "objectID": "12345",
            "title": "x",
            "url": "https://example.com",
            "points": 100,
            "num_comments": 20,
        }
        rec = hit_to_raw_record(hit)
        assert rec.external_id == "12345"
        assert rec.source == "hackernews"

    def test_body_is_verbatim_no_trimming(self) -> None:
        """Locked decision (spec §3): Bronze stores raw, Wave 2 parses.
        Adapter MUST NOT modify, trim, or normalize the hit."""
        long_title = "x" * 500
        hit = {
            "objectID": "1",
            "title": long_title,
            "_tags": ["story", "ask_hn"],
            "story_text": "y" * 1000,
        }
        rec = hit_to_raw_record(hit)
        assert rec.body == hit
        assert rec.body["title"] == long_title  # no trimming
        assert rec.body["story_text"] == "y" * 1000

    def test_ask_hn_post_with_null_url_still_yields_valid_external_id(self) -> None:
        """Ask HN / Show HN text posts often carry a null `url`. We
        rely on objectID for external_id, so dedupe still works.
        Wave 2 handles the permalink fallback."""
        hit = {"objectID": "9876", "title": "Ask HN: ...", "url": None}
        rec = hit_to_raw_record(hit)
        assert rec.external_id == "9876"
        assert rec.body["url"] is None  # verbatim — no fallback in the adapter

    def test_returns_real_raw_record_instance(self) -> None:
        rec = hit_to_raw_record({"objectID": "7", "title": "x"})
        assert isinstance(rec, RawRecord)
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/sources/test_hackernews.py::TestKeepHit tests/unit/sources/test_hackernews.py::TestHitToRawRecord -v
```

Expected: `ImportError: cannot import name 'hit_to_raw_record' from 'discovery.sources.hackernews'`.

- [ ] **Step 3: Implement `keep_hit` + `hit_to_raw_record`**

Add to the top of `src/discovery/sources/hackernews.py` (with the existing imports):

```python
from discovery.sources.base import RawRecord
```

Then append after `build_search_url`:

```python
def keep_hit(hit: dict[str, Any]) -> bool:
    """Adapter-side floor -- near-noop. Server-side `numericFilters`
    does the quality work (spec §11). Locally we only drop hits with
    no `objectID` (impossible per Algolia's docs but cheap defense).
    """
    return hit.get("objectID") is not None


def hit_to_raw_record(hit: dict[str, Any]) -> RawRecord:
    """Convert an Algolia HN hit into a `RawRecord`.

    - `external_id = str(hit["objectID"])` -- HN's permanent story id,
      always present per Algolia's index.
    - `body = hit` verbatim -- Wave 2 parses; spec §3 "Bronze stores
      raw" is a locked decision.
    - No snippet construction, no permalink fallback, no body trimming.
      Those are Wave 2 concerns in this project, even though the HN
      guide discusses them adapter-side.
    """
    return RawRecord(
        source="hackernews",
        external_id=str(hit["objectID"]),
        body=hit,
    )
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/sources/test_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; the 6 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/sources/hackernews.py tests/unit/sources/test_hackernews.py
git commit -m "feat(sources): hackernews keep_hit + hit_to_raw_record (verbatim Bronze)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2.3: `HackerNewsSource` adapter class

**Files:**
- Modify: `src/discovery/sources/hackernews.py` — add `HackerNewsSource(BaseSource)` with `fetch` / `_run_one` / `aclose`.
- Modify: `tests/unit/sources/test_hackernews.py` — add `TestHackerNewsSourceFetch` / `TestHackerNewsSourceAclose` / `TestHackerNewsSourceLogging`.

**Spec reference:** §11. No retry (locked); per-instance limiter (locked); partial-success loop; structured per-query log line.

- [ ] **Step 1: Write the failing tests**

Add to the top imports of `tests/unit/sources/test_hackernews.py`:

```python
from collections.abc import Callable

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.hackernews import HackerNewsSource  # extend existing import line
```

Then append:

```python
def _client_from_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fast_limiter() -> AsyncLimiter:
    """Effectively-unbounded limiter for tests -- production uses
    AsyncLimiter(5, 1) for politeness."""
    return AsyncLimiter(max_rate=1000, time_period=1)


def _mock_hn_response(hit_ids: list[str]) -> dict[str, Any]:
    """Build a minimal Algolia HN response."""
    return {
        "hits": [
            {
                "objectID": hid,
                "title": f"Title {hid}",
                "url": f"https://example.com/{hid}",
                "points": 100,
                "num_comments": 20,
                "author": "alice",
                "created_at": "2026-05-01T00:00:00Z",
                "_tags": ["story"],
            }
            for hid in hit_ids
        ],
        "nbHits": len(hit_ids),
    }


class TestHackerNewsSourceFetch:
    async def test_happy_path_returns_records(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_mock_hn_response(["a1", "a2"]))

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        records = await source.fetch({"queries": [_query()]})

        assert len(records) == 2
        assert all(r.source == "hackernews" for r in records)
        assert {r.external_id for r in records} == {"a1", "a2"}

    async def test_partial_success_returns_what_worked(self) -> None:
        """Locked partial-success contract (spec §11). One failed query
        does not poison the others."""
        counter = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "query=fails" in str(request.url):
                return httpx.Response(500)
            counter["n"] += 1
            return httpx.Response(200, json=_mock_hn_response([f"g{counter['n']}"]))

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        records = await source.fetch(
            {
                "queries": [
                    _query(query="ok"),
                    _query(query="fails"),
                    _query(query="ok2"),
                ],
            }
        )

        assert len(records) == 2  # 2 of 3 queries succeeded

    async def test_all_queries_fail_raises_first_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch({"queries": [_query(), _query(query="another")]})

    async def test_no_retry_on_5xx(self) -> None:
        """Locked decision (spec §11 / §16): HN does NOT retry. A
        single 500 records ONE error; the adapter does not re-hit the
        URL -- this divergence from the source-adapter umbrella is
        deliberate."""
        hit_count = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            hit_count["n"] += 1
            return httpx.Response(500)

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch({"queries": [_query()]})

        assert hit_count["n"] == 1  # exactly one HTTP call

    async def test_filters_hits_without_object_id(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {"objectID": "g1", "title": "good"},
                        {"title": "no objectID -- dropped"},
                        {"objectID": "g2", "title": "good"},
                    ],
                    "nbHits": 3,
                },
            )

        source = HackerNewsSource(client=_client_from_handler(handler), limiter=_fast_limiter())
        records = await source.fetch({"queries": [_query()]})

        assert len(records) == 2
        assert {r.external_id for r in records} == {"g1", "g2"}


class TestHackerNewsSourceAclose:
    async def test_aclose_closes_owned_client(self) -> None:
        source = HackerNewsSource(limiter=_fast_limiter())  # owns its own client
        assert not source._client.is_closed
        await source.aclose()
        assert source._client.is_closed

    async def test_aclose_does_not_close_injected_client(self) -> None:
        injected = httpx.AsyncClient()
        try:
            source = HackerNewsSource(client=injected, limiter=_fast_limiter())
            await source.aclose()
            assert not injected.is_closed
        finally:
            # Test owns the injected client; close it ourselves so
            # `filterwarnings=error` doesn't trip on GC.
            await injected.aclose()


class TestHackerNewsSourceLogging:
    async def test_per_query_log_line_carries_diagnostic_fields(self) -> None:
        """Spec §11 / skill item 21 analog: per-query log line carries
        url, status, response time, count before AND after filter,
        endpoint, tags."""
        captured: list[dict[str, Any]] = []

        def sink(message: Any) -> None:
            captured.append(dict(message.record["extra"]))

        sink_id = logger.add(sink, level="DEBUG")
        try:
            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_mock_hn_response(["g1", "g2"]))

            source = HackerNewsSource(
                client=_client_from_handler(handler), limiter=_fast_limiter()
            )
            await source.fetch(
                {"queries": [_query(endpoint="search_by_date", tags="show_hn")]}
            )

            query_logs = [c for c in captured if "url" in c and "count_after_filter" in c]
            assert query_logs, f"no per-query log line found; captured: {captured}"
            log = query_logs[0]
            assert log["status"] == 200
            assert log["count_before_filter"] == 2
            assert log["count_after_filter"] == 2
            assert log["endpoint"] == "search_by_date"
            assert log["tags"] == "show_hn"
            assert log["elapsed_ms"] >= 0
            assert "hn.algolia.com/api/v1/search_by_date" in log["url"]
        finally:
            logger.remove(sink_id)
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/sources/test_hackernews.py -v
```

Expected: `ImportError: cannot import name 'HackerNewsSource' from 'discovery.sources.hackernews'`.

- [ ] **Step 3: Implement `HackerNewsSource`**

Add to the top imports of `src/discovery/sources/hackernews.py` (alongside the existing imports). Final import block:

```python
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.base import BaseSource, RawRecord
```

(`RawRecord` was added in Task 2.2; extend that line to also include `BaseSource`. `time` is new.)

Then append the class after `hit_to_raw_record`:

```python
class HackerNewsSource(BaseSource):
    """HN source adapter via the Algolia HN Search API.

    No auth, no User-Agent requirement, generous rate limits.

    Constructor parameters
    ----------------------
    client :
        Optional pre-built `httpx.AsyncClient`. If omitted, a fresh one
        is created. Tests inject a client backed by `httpx.MockTransport`.
    limiter :
        Optional `AsyncLimiter`. Default = a fresh per-instance limiter
        (5 req/s polite). Per-instance, NOT a process-wide singleton:
        only one HN consumer exists in this project, unlike Reddit
        which has two sharing a 10/min budget (spec §11).
    timeout :
        httpx client timeout when we create the client ourselves.

    No retry -- see spec §11 / §16. One GET per query; non-2xx or
    network errors are recorded per-query and the loop continues
    (partial success). When every query fails, the first error is
    re-raised so the worker can mark the task failed.
    """

    name = "hackernews"
    rate_limit = (5, 1)   # 5 req/s polite -- Algolia ceiling is ~10k/hr

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        self._owned_client = client is None
        self._limiter = (
            limiter if limiter is not None else AsyncLimiter(max_rate=5, time_period=1)
        )

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        """Run every query in `params['queries']` and collect results.

        Partial success per spec §11: a failed query does not poison
        the others. If every query fails, the first error is re-raised.
        No retry (locked divergence from the source-adapter umbrella).
        """
        records: list[RawRecord] = []
        errors: list[Exception] = []
        for q in params.get("queries", []):
            try:
                page_records = await self._run_one(q)
                records.extend(page_records)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("hn query failed", query=q, error=str(exc))
                errors.append(exc)

        if not records and errors:
            raise errors[0]
        return records

    async def _run_one(self, query: dict[str, Any]) -> list[RawRecord]:
        url = build_search_url(query)
        started_at = time.monotonic()
        async with self._limiter:
            response = await self._client.get(url)
        elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
        response.raise_for_status()
        payload = response.json()
        hits = payload.get("hits", [])

        out: list[RawRecord] = []
        for hit in hits:
            if keep_hit(hit):
                out.append(hit_to_raw_record(hit))

        # Skill item 21 analog -- per-query diagnostic line.
        logger.info(
            "hn query done",
            url=url,
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            count_before_filter=len(hits),
            count_after_filter=len(out),
            endpoint=query.get("endpoint"),
            tags=query.get("tags"),
        )
        return out

    async def aclose(self) -> None:
        """Close the underlying HTTP client if we created it."""
        if self._owned_client:
            await self._client.aclose()
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/sources/test_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; ~10 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/sources/hackernews.py tests/unit/sources/test_hackernews.py
git commit -m "feat(sources): HackerNewsSource adapter (no retry, partial success)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

**End of Chunk 2.** The HN source adapter is testable in isolation: pure URL builder, pure hit converters, the adapter class with partial-success semantics and an aclose-able owned client. Nothing has wired it into the worker or the Wave-0 plan yet — that's Chunks 3-5.

---

## Chunk 3: HN orchestrator

Four tasks that build `src/discovery/orchestrator/hackernews.py` — the bridge between the LLM's `HackerNewsKeywordSpec` candidates (or the no-LLM template) and the adapter's compiled fetch-params dict. Every brittle mechanical rule from the spec lives in tested Python here. Mirrors `src/discovery/orchestrator/reddit.py` in structure where the locked decisions allow. Use @superpowers:test-driven-development for every task.

The end-of-chunk file shape:

```
src/discovery/orchestrator/hackernews.py
├── _TIME_WINDOW_SECONDS, _ROUTING tables (module-level constants)
├── MAX_HN_QUERIES = 6
├── _time_window_epoch(time_window, as_of) -> int | None
├── _routing_for(intent) -> (endpoint, tags, extra_filters)
├── _build_fetch_params(tokens, endpoint, tags, numeric_filters) -> dict
├── _compile_hn_queries(specs, job_spec) -> list[dict]
├── hn_keyword_candidates_for_spec(spec) -> list[dict]    # template fallback
├── _queries_from_job_plan(job) -> list[dict] | None
└── enqueue_hn_task_for_job(session, job) -> Task         # public entry
```

### Task 3.1: `_time_window_epoch` + `_routing_for` deterministic mappings

**Files:**
- Create: `src/discovery/orchestrator/hackernews.py` (initial — module docstring, constants, two helpers).
- Create: `tests/unit/test_orchestrator_hackernews.py` (initial — imports + `TestTimeWindowEpoch` + `TestRoutingFor`).

**Spec reference:** §10 routing table + time-window table.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_orchestrator_hackernews.py`:

```python
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from discovery.orchestrator.hackernews import _routing_for, _time_window_epoch


def _epoch(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp())


class TestTimeWindowEpoch:
    def test_hour_window(self) -> None:
        anchor = date(2026, 5, 20)
        # Anchor at 2026-05-20 00:00 UTC; subtract 1 hour -> 2026-05-19 23:00 UTC.
        assert _time_window_epoch("hour", anchor) == _epoch(2026, 5, 19, 23)

    def test_day_window(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("day", anchor) == _epoch(2026, 5, 19)

    def test_week_window(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("week", anchor) == _epoch(2026, 5, 13)

    def test_month_window_30_days(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("month", anchor) == _epoch(2026, 4, 20)

    def test_year_window_365_days(self) -> None:
        anchor = date(2026, 5, 20)
        assert _time_window_epoch("year", anchor) == _epoch(2025, 5, 20)

    def test_all_returns_none(self) -> None:
        """`all` -> None signals 'omit created_at_i entirely from numericFilters'."""
        assert _time_window_epoch("all", date(2026, 5, 20)) is None

    def test_unknown_window_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown time window"):
            _time_window_epoch("decade", date(2026, 5, 20))


class TestRoutingFor:
    def test_launch_routes_to_search_by_date_show_hn_relaxed(self) -> None:
        endpoint, tags, extra = _routing_for("launch")
        assert endpoint == "search_by_date"
        assert tags == "show_hn"
        assert extra == []  # no points/comments floor -- recency is the signal

    def test_context_routes_to_search_story_with_quality_floor(self) -> None:
        endpoint, tags, extra = _routing_for("context")
        assert endpoint == "search"
        assert tags == "story"
        assert extra == ["points>5", "num_comments>3"]
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py -v
```

Expected: `ModuleNotFoundError: No module named 'discovery.orchestrator.hackernews'`.

- [ ] **Step 3: Implement the helpers**

Create `src/discovery/orchestrator/hackernews.py`:

```python
"""Wave 1 orchestration for HackerNews.

Bridges the Wave 0 LLM output (`JobPlan.hn_queries`) and the HN adapter's
fetch-params dict. Every brittle mechanical rule from the design spec
lives here in tested Python:

- Token decomposition (delegated to `discovery.sources.keyword_tokens`).
- Endpoint + tag routing from the LLM's per-candidate `intent` flag.
- Server-side `numericFilters` from `JobSpec.time_window` and `as_of`.
- The `MAX_HN_QUERIES=6` cap.

When `Job.job_plan` is null (Wave 0 failed) or fails validation, falls
back to the deterministic capability-first template so HN keeps working
with `OPENAI_API_KEY` unset -- mirroring Reddit's template fallback.

See `docs/specs/2026-05-20-hackernews-source-design.md` §10.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

_TIME_WINDOW_SECONDS: dict[str, int] = {
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 30 * 86_400,   # 2,592,000
    "year": 365 * 86_400,   # 31,536,000
}

# Routing table -- the deterministic 2:1 launch/context split that
# Python owns. Each entry maps an intent flag to (endpoint, tags,
# extra_numeric_filters). Created_at_i is layered on top by
# `_compile_hn_queries` from the JobSpec time window.
_ROUTING: dict[str, tuple[str, str, list[str]]] = {
    "launch":  ("search_by_date", "show_hn", []),
    "context": ("search",         "story",   ["points>5", "num_comments>3"]),
}


def _time_window_epoch(time_window: str, as_of: date) -> int | None:
    """Compute the unix-seconds floor for `created_at_i` from the job's
    time window, anchored at `as_of` midnight UTC.

    `all` -> None (caller omits `created_at_i` entirely from
    numericFilters; the rest of the filter list still applies).

    `hour | day | week | month | year` -> integer epoch seconds.
    """
    if time_window == "all":
        return None
    if time_window not in _TIME_WINDOW_SECONDS:
        raise ValueError(f"unknown time window: {time_window!r}")
    anchor = datetime.combine(as_of, time.min, tzinfo=UTC)
    floor = anchor - timedelta(seconds=_TIME_WINDOW_SECONDS[time_window])
    return int(floor.timestamp())


def _routing_for(intent: str) -> tuple[str, str, list[str]]:
    """Map an intent flag to (endpoint, tags, extra_numeric_filters).

    Raises KeyError on unknown intent -- the LLM contract is enforced
    by the `HackerNewsKeywordSpec.intent` Literal, so an unknown value
    indicates a contract violation upstream.
    """
    return _ROUTING[intent]
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; the 9 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/orchestrator/hackernews.py tests/unit/test_orchestrator_hackernews.py
git commit -m "feat(orchestrator): HN time_window->epoch + intent->routing mappings" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3.2: `_compile_hn_queries` pipeline + `_build_fetch_params`

**Files:**
- Modify: `src/discovery/orchestrator/hackernews.py` — add `MAX_HN_QUERIES`, `_build_fetch_params`, `_compile_hn_queries`.
- Modify: `tests/unit/test_orchestrator_hackernews.py` — add `TestCompileHnQueries` + helpers.

**Spec reference:** §10 (compile pipeline: decompose -> dedupe -> route -> numericFilters -> cap).

- [ ] **Step 1: Write the failing tests**

Add to the top imports of `tests/unit/test_orchestrator_hackernews.py`:

```python
from discovery.jobs import JobSpec
from discovery.llm.schemas import HackerNewsKeywordSpec
from discovery.orchestrator.hackernews import (
    MAX_HN_QUERIES,
    _compile_hn_queries,
    _routing_for,        # already imported
    _time_window_epoch,  # already imported
)
```

Then append helpers + the test class:

```python
def _kw(keyword: str, intent: str = "launch") -> HackerNewsKeywordSpec:
    return HackerNewsKeywordSpec(
        keyword=keyword,
        intent=intent,  # type: ignore[arg-type]
        rationale="test",
    )


def _spec(industry: str = "test industry", time_window: str = "month") -> JobSpec:
    return JobSpec(
        industry=industry,
        as_of=date(2026, 5, 20),
        time_window=time_window,  # type: ignore[arg-type]
    )


class TestCompileHnQueries:
    def test_decomposes_each_keyword_to_two_tokens(self) -> None:
        out = _compile_hn_queries([_kw("Personal CRM local-first")], _spec())
        assert len(out) == 1
        # "local-first" at position 3 is dropped by decompose_keyword.
        assert out[0]["query"] == "Personal CRM"

    def test_drops_empty_decomposition(self) -> None:
        # All-stopwords keyword decomposes to [] -> dropped silently.
        out = _compile_hn_queries([_kw("the a an")], _spec())
        assert out == []

    def test_dedupes_on_token_tuple(self) -> None:
        # Same keyword twice -> compiled once.
        out = _compile_hn_queries([_kw("MCP server"), _kw("MCP server")], _spec())
        assert len(out) == 1

    def test_dedup_is_case_sensitive(self) -> None:
        # `MCP` and `mcp` are different on HN (acronym casing matters).
        out = _compile_hn_queries([_kw("MCP server"), _kw("mcp server")], _spec())
        assert len(out) == 2

    def test_routes_launch_to_search_by_date_show_hn(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI", intent="launch")], _spec())
        assert out[0]["endpoint"] == "search_by_date"
        assert out[0]["tags"] == "show_hn"

    def test_routes_context_to_search_with_quality_floor(self) -> None:
        out = _compile_hn_queries([_kw("CRM founder", intent="context")], _spec())
        assert out[0]["endpoint"] == "search"
        assert out[0]["tags"] == "story"
        assert "points>5" in out[0]["numeric_filters"]
        assert "num_comments>3" in out[0]["numeric_filters"]

    def test_includes_created_at_i_when_time_window_is_not_all(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI")], _spec(time_window="month"))
        assert "created_at_i>" in out[0]["numeric_filters"]

    def test_omits_all_filters_for_launch_plus_all_time_window(self) -> None:
        """Launch intent + time_window='all' -> no quality floor and no
        recency floor -> numeric_filters is the empty string."""
        out = _compile_hn_queries(
            [_kw("CRM CLI", intent="launch")], _spec(time_window="all")
        )
        assert out[0]["numeric_filters"] == ""

    def test_context_with_all_time_keeps_quality_floor(self) -> None:
        out = _compile_hn_queries(
            [_kw("CRM founder", intent="context")], _spec(time_window="all")
        )
        # No created_at_i but points/num_comments still apply.
        assert "created_at_i" not in out[0]["numeric_filters"]
        assert "points>5" in out[0]["numeric_filters"]
        assert "num_comments>3" in out[0]["numeric_filters"]

    def test_caps_at_max_hn_queries_preserving_llm_order(self) -> None:
        kws = [_kw(f"kw{i} tok") for i in range(15)]
        out = _compile_hn_queries(kws, _spec())
        assert len(out) == MAX_HN_QUERIES == 6
        # Order preserved -- LLM ranking signal (spec §8).
        assert out[0]["query"] == "kw0 tok"
        assert out[5]["query"] == "kw5 tok"

    def test_hits_per_page_is_30(self) -> None:
        out = _compile_hn_queries([_kw("CRM CLI")], _spec())
        assert out[0]["hits_per_page"] == 30

    def test_query_is_space_joined_tokens(self) -> None:
        # Tokens joined by single space -- HN treats whitespace as token-AND.
        out = _compile_hn_queries([_kw("CRM CLI")], _spec())
        assert out[0]["query"] == "CRM CLI"

    def test_created_at_i_appears_first_in_filter_string(self) -> None:
        out = _compile_hn_queries(
            [_kw("CRM founder", intent="context")], _spec(time_window="month")
        )
        # Convention: time filter first, quality filters after.
        filters = out[0]["numeric_filters"]
        assert filters.startswith("created_at_i>")
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py::TestCompileHnQueries -v
```

Expected: `ImportError` on `MAX_HN_QUERIES` / `_compile_hn_queries`.

- [ ] **Step 3: Implement the pipeline**

Add to top imports of `src/discovery/orchestrator/hackernews.py`:

```python
from collections.abc import Iterable
from typing import Any

from discovery.jobs import JobSpec
from discovery.llm.schemas import HackerNewsKeywordSpec
from discovery.sources.keyword_tokens import decompose_keyword
```

Then append after `_routing_for`:

```python
MAX_HN_QUERIES: int = 6


def _build_fetch_params(
    query_tokens: list[str],
    endpoint: str,
    tags: str,
    numeric_filters: str,
) -> dict[str, Any]:
    """Assemble the per-query dict the HN adapter consumes (spec §10)."""
    return {
        "endpoint": endpoint,
        "query": " ".join(query_tokens),
        "tags": tags,
        "numeric_filters": numeric_filters,
        "hits_per_page": 30,
    }


def _compile_hn_queries(
    specs: Iterable[HackerNewsKeywordSpec],
    job_spec: JobSpec,
) -> list[dict[str, Any]]:
    """Decompose -> dedupe -> route -> numericFilters -> cap. Pure
    function. Preserves the LLM's emission order (a ranking signal).
    """
    epoch = _time_window_epoch(job_spec.time_window, job_spec.as_of)
    seen_tokens: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []

    for spec in specs:
        tokens = decompose_keyword(spec.keyword)
        if not tokens:
            continue
        token_key = tuple(tokens)
        if token_key in seen_tokens:
            continue
        seen_tokens.add(token_key)

        endpoint, tags, extra_filters = _routing_for(spec.intent)
        filters: list[str] = []
        if epoch is not None:
            filters.append(f"created_at_i>{epoch}")
        filters.extend(extra_filters)
        numeric_filters = ",".join(filters)

        out.append(_build_fetch_params(tokens, endpoint, tags, numeric_filters))
        if len(out) >= MAX_HN_QUERIES:
            break

    return out
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; 13 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/orchestrator/hackernews.py tests/unit/test_orchestrator_hackernews.py
git commit -m "feat(orchestrator): _compile_hn_queries pipeline (decompose+route+cap)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3.3: `hn_keyword_candidates_for_spec` template fallback

**Files:**
- Modify: `src/discovery/orchestrator/hackernews.py` — add the public template helper.
- Modify: `tests/unit/test_orchestrator_hackernews.py` — add `TestHnKeywordCandidatesForSpec`.

**Spec reference:** §10 ("Template fallback"). Capability word FIRST in keyword so it survives decomposition for multi-word industries.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_orchestrator_hackernews.py` (extend the existing import line to add `hn_keyword_candidates_for_spec`):

```python
class TestHnKeywordCandidatesForSpec:
    def test_returns_at_least_one_compiled_query(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning"))
        # Template has 4 candidates -- all should compile cleanly for a
        # single-word industry.
        assert len(out) >= 1

    def test_capability_first_survives_decomposition_for_multiword_industry(self) -> None:
        """For 'commercial cleaning', the template uses `CLI commercial
        cleaning` etc. so the capability word lands in position 1 and
        survives the 2-token cap."""
        out = hn_keyword_candidates_for_spec(_spec(industry="commercial cleaning"))
        queries = [q["query"] for q in out]
        # Every query starts with a capability word (CLI/OSS/API/workflow)
        # followed by the first industry word.
        assert any(q.startswith("CLI ") for q in queries)
        assert any(q.startswith("OSS ") for q in queries)
        assert any(q.startswith("API ") for q in queries)
        assert any(q.startswith("workflow ") for q in queries)

    def test_includes_both_launch_and_context_queries(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning"))
        endpoints = {q["endpoint"] for q in out}
        # CLI/OSS/API are launch, workflow is context.
        assert "search_by_date" in endpoints  # at least one launch
        assert "search" in endpoints           # at least one context

    def test_each_query_carries_the_time_window_filter(self) -> None:
        out = hn_keyword_candidates_for_spec(_spec(industry="cleaning", time_window="year"))
        for q in out:
            # year window -> created_at_i floor present on every query.
            assert "created_at_i>" in q["numeric_filters"]

    def test_template_dedupes_via_compile_pipeline(self) -> None:
        """If the industry word collides with a capability word (unlikely
        but possible), the compile pipeline still dedupes by token tuple."""
        # Just a smoke check that calling twice with the same spec yields
        # the same compiled output -- deterministic and pure.
        spec = _spec(industry="cleaning")
        assert hn_keyword_candidates_for_spec(spec) == hn_keyword_candidates_for_spec(spec)
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py::TestHnKeywordCandidatesForSpec -v
```

Expected: `ImportError` on `hn_keyword_candidates_for_spec`.

- [ ] **Step 3: Implement the template**

Append to `src/discovery/orchestrator/hackernews.py`:

```python
def hn_keyword_candidates_for_spec(spec: JobSpec) -> list[dict[str, Any]]:
    """Deterministic HN fallback -- no LLM. Capability word FIRST so
    decomposition keeps it for multi-word industries (e.g.
    `commercial cleaning CLI` would drop `CLI`; `CLI commercial
    cleaning` keeps `CLI` + the first industry word). Same compile
    path as the LLM output.

    Used when `Job.job_plan` is null (Wave 0 failed or `OPENAI_API_KEY`
    unset). Mirrors `orchestrator.reddit.reddit_queries_for_spec`.
    """
    industry = spec.industry
    candidates = [
        HackerNewsKeywordSpec(
            keyword=f"CLI {industry}",
            intent="launch",
            rationale="(template) CLI launch fallback",
        ),
        HackerNewsKeywordSpec(
            keyword=f"OSS {industry}",
            intent="launch",
            rationale="(template) OSS launch fallback",
        ),
        HackerNewsKeywordSpec(
            keyword=f"API {industry}",
            intent="launch",
            rationale="(template) API launch fallback",
        ),
        HackerNewsKeywordSpec(
            keyword=f"workflow {industry}",
            intent="context",
            rationale="(template) workflow discussion fallback",
        ),
    ]
    return _compile_hn_queries(candidates, spec)
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; 5 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/orchestrator/hackernews.py tests/unit/test_orchestrator_hackernews.py
git commit -m "feat(orchestrator): HN deterministic template fallback (capability-first)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3.4: `_queries_from_job_plan` + `enqueue_hn_task_for_job`

**Files:**
- Modify: `src/discovery/orchestrator/hackernews.py` — add the two public-ish entries.
- Modify: `tests/unit/test_orchestrator_hackernews.py` — add `TestQueriesFromJobPlan` + `TestEnqueueHnTaskForJob`.

**Spec reference:** §10 (template-fallback trigger), §3 (`(job_id, content_hash)` UNIQUE). Mirrors `orchestrator/reddit.py::enqueue_reddit_task_for_job` for idempotency.

**Read first:** `src/discovery/orchestrator/reddit.py` to confirm the exact mirror pattern (`enqueue_reddit_task_for_job` is the template). Use the same `session.exec(select(Task).where(...)).first()` pre-check + `hash_params({"source": ..., "action": ..., "params": params})` pattern.

**Also read:** `tests/unit/test_orchestrator_reddit.py` (lines 28-36) to see the existing async `session` fixture. The project convention is per-file (no conftest sharing), so this task copies that fixture block verbatim into `tests/unit/test_orchestrator_hackernews.py` — see Step 1 below.

- [ ] **Step 1: Write the failing tests**

Add to top imports of `tests/unit/test_orchestrator_hackernews.py`:

```python
from collections.abc import AsyncIterator

from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db import models  # noqa: F401 -- registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, Task
from discovery.llm.schemas import JobPlan, RedditQuerySpec
from discovery.orchestrator.hackernews import (
    _queries_from_job_plan,
    enqueue_hn_task_for_job,
    hn_keyword_candidates_for_spec,  # already imported in Task 3.3
)
```

Then add the per-file `session` fixture (copied verbatim from `tests/unit/test_orchestrator_reddit.py:28-36` — the project convention is per-file, no conftest sharing):

```python
@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_session_factory(engine)
    async with maker() as sess:
        yield sess
    await engine.dispose()
```

Then append helpers + tests:

```python
def _make_reddit_queries(n: int = 25) -> list[RedditQuerySpec]:
    """25 valid RedditQuerySpec to satisfy JobPlan's 25-30 band."""
    return [
        RedditQuerySpec(
            endpoint="site_wide",
            q=f'(subreddit:startups) AND "test{i}"',
            sort="top",
            t="month",
            limit=100,
            rationale="test",
        )
        for i in range(n)
    ]


def _make_job(
    *,
    industry: str = "cleaning",
    time_window: str = "month",
    job_plan: dict[str, Any] | None = None,
) -> Job:
    spec = JobSpec(
        industry=industry,
        as_of=date(2026, 5, 20),
        time_window=time_window,  # type: ignore[arg-type]
    )
    return Job(
        spec=spec.model_dump(mode="json"),
        spec_hash="testhash",
        job_plan=job_plan,
    )


class TestQueriesFromJobPlan:
    def test_returns_none_when_job_plan_is_null(self) -> None:
        job = _make_job(job_plan=None)
        assert _queries_from_job_plan(job) is None

    def test_returns_empty_list_when_hn_queries_is_empty(self) -> None:
        """Permissive default: LLM intentionally emitted [] (graceful
        sparsity per §8 / §17). Return [] -- caller does NOT fall back
        to template, because the LLM deliberately said nothing."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), hn_queries=[])
        job = _make_job(job_plan=plan.model_dump())
        assert _queries_from_job_plan(job) == []

    def test_compiles_when_hn_queries_present(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(
                    keyword="CRM CLI", intent="launch", rationale="r",
                ),
            ],
        )
        job = _make_job(job_plan=plan.model_dump())
        out = _queries_from_job_plan(job)
        assert out is not None
        assert len(out) == 1
        assert out[0]["tags"] == "show_hn"

    def test_returns_none_on_validation_failure(self) -> None:
        """If `job_plan` is set but its shape doesn't validate, fall back
        to template (None signal) -- defensive against schema drift."""
        job = _make_job(job_plan={"reddit_queries": "wrong shape"})
        assert _queries_from_job_plan(job) is None


class TestEnqueueHnTaskForJob:
    async def test_creates_task_with_compiled_queries_when_plan_present(
        self, session: AsyncSession
    ) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[
                HackerNewsKeywordSpec(
                    keyword="CRM CLI", intent="launch", rationale="r",
                ),
            ],
        )
        job = _make_job(job_plan=plan.model_dump())
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_hn_task_for_job(session, job)

        assert task.id is not None
        assert task.job_id == job.id
        assert task.source == "hackernews"
        assert task.action == "fetch"
        assert len(task.params["queries"]) == 1
        assert task.params["queries"][0]["tags"] == "show_hn"

    async def test_falls_back_to_template_when_job_plan_null(
        self, session: AsyncSession
    ) -> None:
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_hn_task_for_job(session, job)

        # Template emits 4 candidates; compile pipeline keeps all 4.
        assert len(task.params["queries"]) == 4
        endpoints = {q["endpoint"] for q in task.params["queries"]}
        assert "search_by_date" in endpoints
        assert "search" in endpoints

    async def test_creates_task_even_when_hn_queries_intentionally_empty(
        self, session: AsyncSession
    ) -> None:
        """Empty `hn_queries` (LLM said 'this industry has no HN signal')
        creates a no-op task -- graceful sparsity. The task runs, fetches
        zero records, completes `done`."""
        plan = JobPlan(reddit_queries=_make_reddit_queries(), hn_queries=[])
        job = _make_job(job_plan=plan.model_dump())
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = await enqueue_hn_task_for_job(session, job)

        assert task.params["queries"] == []

    async def test_idempotent_on_content_hash(self, session: AsyncSession) -> None:
        """Re-enqueuing the same job returns the existing task (UNIQUE on
        (job_id, content_hash)). No duplicate Bronze fetches."""
        job = _make_job(job_plan=None)
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task_a = await enqueue_hn_task_for_job(session, job)
        task_b = await enqueue_hn_task_for_job(session, job)

        assert task_a.id == task_b.id
```

The `session` fixture is the one copied verbatim in the imports/fixture block above. If a future refactor moves the fixture into a `tests/unit/conftest.py`, adjust this file to drop the local copy — until then, every orchestrator test file that touches the DB carries its own copy.

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py -v
```

Expected: `ImportError` on `_queries_from_job_plan` / `enqueue_hn_task_for_job`.

- [ ] **Step 3: Implement both functions**

Add to top imports of `src/discovery/orchestrator/hackernews.py`:

```python
from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job, Task
from discovery.hashing import hash_params
from discovery.llm.schemas import JobPlan
```

Then append:

```python
def _queries_from_job_plan(job: Job) -> list[dict[str, Any]] | None:
    """Extract compiled HN queries from a populated `job_plan`, or
    return None to signal "use the template instead."

    Returns:
    - `None`  when `job.job_plan` is null OR fails JobPlan validation
      (template fallback signal).
    - `[]`    when `job_plan` is valid but `hn_queries` is empty (LLM
      intentionally emitted nothing -- graceful sparsity; do NOT fall
      back to template).
    - `[...]` when `hn_queries` is non-empty (compile pipeline applied).
    """
    if job.job_plan is None:
        return None
    try:
        plan = JobPlan.model_validate(job.job_plan)
    except Exception as e:
        logger.warning(
            "job {} has a job_plan that fails validation ({}); falling back to HN template.",
            job.id,
            e,
        )
        return None
    spec = JobSpec.model_validate(job.spec)
    return _compile_hn_queries(plan.hn_queries, spec)


async def enqueue_hn_task_for_job(session: AsyncSession, job: Job) -> Task:
    """Queue one HN fetch task for `job`. Idempotent on `content_hash`.

    Query source priority:

    1. `job.job_plan["hn_queries"]` (Wave 0 LLM output), compiled.
    2. `hn_keyword_candidates_for_spec(spec)` -- the deterministic
       template -- when Wave 0 didn't run or its plan failed
       validation.

    An empty compiled list is intentional (graceful HN sparsity on
    non-tech industries -- spec §17 risk 5) and DOES enqueue a task;
    the task runs, fetches zero records, and completes `done`. Mirrors
    `orchestrator.reddit.enqueue_reddit_task_for_job` for shape.
    """
    spec = JobSpec.model_validate(job.spec)
    queries = _queries_from_job_plan(job)
    if queries is None:
        # Note: `is None`, not `or` -- an empty `hn_queries` from the LLM
        # is a valid output (graceful HN sparsity per spec §17 risk 5);
        # only a missing or invalid job_plan triggers the template fallback.
        queries = hn_keyword_candidates_for_spec(spec)
    params: dict[str, Any] = {"queries": queries}
    content_hash = hash_params(
        {"source": "hackernews", "action": "fetch", "params": params}
    )

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
        source="hackernews",
        action="fetch",
        params=params,
        content_hash=content_hash,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/test_orchestrator_hackernews.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green; 8 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/orchestrator/hackernews.py tests/unit/test_orchestrator_hackernews.py
git commit -m "feat(orchestrator): enqueue_hn_task_for_job (idempotent, template fallback)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

**End of Chunk 3.** The HN orchestrator is fully testable in isolation: every brittle mechanical rule (decomposition, dedupe, 2:1 routing, numericFilters assembly, ≤6 cap, template fallback, idempotent enqueue) lives in tested Python. The only remaining wires are the Wave-0 prompt + station carry-through (Chunk 4) and the CLI/worker integration (Chunk 5).

---

## Chunk 4: Wave-0 v6 prompt + carry-through

Two tasks. Task 4.1 ships the carry-through helper + wiring FIRST so the locked Reddit tail can never silently eat an LLM-emitted `hn_queries`. Task 4.2 then bumps the prompt to v6 and teaches the LLM how to produce HN-suitable candidates. Order matters — if the prompt landed first, the LLM's `hn_queries` would be dropped on the floor by the tail.

The locked Reddit tail uses `JobPlan.model_construct(reddit_queries=..., reddit_subreddits=...)` at four sites (`_ground_selection`, `_force_time_window`, `_merge_baseline_subreddits`, `_drop_invalid_queries`). `model_construct` keeps only the fields passed; non-Reddit fields are dropped. The carry-through is a one-line capture before the tail + one-line restore after.

Use @superpowers:test-driven-development for every task.

### Task 4.1: `_attach_hn_queries` helper + `run_query_expansion` wiring

**Files:**
- Modify: `src/discovery/llm/stations/query_expansion.py` — add `_attach_hn_queries` helper + 2 wiring lines in `run_query_expansion` + the `HackerNewsKeywordSpec` import.
- Modify: `tests/unit/llm/stations/test_query_expansion.py` — add `TestAttachHnQueries` (pure) and `TestRunQueryExpansionCarriesHnQueries` (integration via the existing mocking scaffold).

**Spec reference:** §6.

- [ ] **Step 1: Write the failing tests**

Extend the existing imports in `tests/unit/llm/stations/test_query_expansion.py`:

```python
from discovery.llm.schemas import HackerNewsKeywordSpec  # add
from discovery.llm.stations.query_expansion import _attach_hn_queries  # add
```

Append helpers + test classes (the file already has `_query`, `_plan`, `_candidates`, `_make_call_openai`, `_make_search` from the existing v5 tests — reuse them):

```python
def _hn_kw(keyword: str = "CRM CLI", intent: str = "launch") -> HackerNewsKeywordSpec:
    return HackerNewsKeywordSpec(
        keyword=keyword,
        intent=intent,  # type: ignore[arg-type]
        rationale="test rationale",
    )


class TestAttachHnQueries:
    """The locked tail's `model_construct` rebuilds drop `hn_queries`.
    `_attach_hn_queries` is the single point that restores them. The
    helper itself must be a pure restore -- no validation, no mutation
    of the plan's Reddit fields.
    """

    def test_attaches_hn_to_a_post_tail_plan(self) -> None:
        # A plan as it emerges from the tail: hn_queries dropped to default.
        post_tail = JobPlan.model_construct(
            reddit_queries=[_query("q1")],
            reddit_subreddits=["startups"],
        )
        # Sanity: tail-style construct leaves hn_queries at its default.
        assert post_tail.hn_queries == []

        hn = [_hn_kw("CRM CLI"), _hn_kw("CRM founder", intent="context")]
        final = _attach_hn_queries(post_tail, hn)

        assert final.hn_queries == hn

    def test_preserves_reddit_fields_unchanged(self) -> None:
        post_tail = JobPlan.model_construct(
            reddit_queries=[_query("q1"), _query("q2")],
            reddit_subreddits=["a", "b", "c"],
        )
        final = _attach_hn_queries(post_tail, [_hn_kw()])

        assert final.reddit_queries == post_tail.reddit_queries
        assert final.reddit_subreddits == ["a", "b", "c"]

    def test_uses_model_construct_skipping_band_validation(self) -> None:
        """Like the rest of the tail, the helper uses `model_construct`
        so a post-pruning plan with FEWER than 25 reddit_queries
        (`_drop_invalid_queries` can prune below band) still survives
        -- the 'too few survived' case is caught upstream in `_finalize`."""
        below_band = JobPlan.model_construct(
            reddit_queries=[_query("q1")],  # just 1 -- below the 25-30 band
            reddit_subreddits=[],
        )
        final = _attach_hn_queries(below_band, [_hn_kw()])

        # No ValidationError raised; the pruned plan survives.
        assert len(final.reddit_queries) == 1
        assert len(final.hn_queries) == 1

    def test_empty_hn_list_yields_empty_hn_queries(self) -> None:
        post_tail = JobPlan.model_construct(
            reddit_queries=[_query("q1")],
            reddit_subreddits=[],
        )
        final = _attach_hn_queries(post_tail, [])
        assert final.hn_queries == []


class TestRunQueryExpansionCarriesHnQueries:
    """Integration: with the carry-through in place, hn_queries emitted
    by the LLM survive the locked Reddit tail and appear in the final
    cached JobPlan that `plan_job` writes to `Job.job_plan`.

    Uses the existing `tmp_cache` fixture (patches `station._cache` AND
    closes it on teardown -- critical, since `filterwarnings = ["error"]`
    would turn an unclosed diskcache into a test failure) and the
    existing `spec` fixture. Same shape as the other test classes in
    this file (e.g. `TestCacheHit`).
    """

    async def test_run_preserves_hn_queries_from_llm_output_to_final_plan(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hn = [
            _hn_kw("CRM CLI", intent="launch"),
            _hn_kw("CRM founder", intent="context"),
        ]
        # Build the plan the mocked LLM will "emit" (28 reddit queries
        # to stay in the 25-30 band after the tail's pruning).
        emitted = JobPlan(
            reddit_queries=[_query(f"q{i}") for i in range(28)],
            reddit_subreddits=["startups"],
            hn_queries=hn,
        )

        monkeypatch.setattr(station, "call_openai", _make_call_openai(emitted))
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))

        final = await run_query_expansion(spec)

        # The 4 model_construct sites in the locked tail would have
        # dropped hn_queries; the carry-through restores them.
        assert final.hn_queries == hn

    async def test_run_with_empty_hn_queries_still_runs_clean(
        self,
        tmp_cache: Cache,
        spec: JobSpec,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the LLM emits `hn_queries=[]` (graceful sparsity), the
        carry-through is a no-op restore -- final plan has empty list."""
        emitted = _plan(n=28)  # no hn_queries -> defaults to []

        monkeypatch.setattr(station, "call_openai", _make_call_openai(emitted))
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))

        final = await run_query_expansion(spec)

        assert final.hn_queries == []
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/llm/stations/test_query_expansion.py::TestAttachHnQueries tests/unit/llm/stations/test_query_expansion.py::TestRunQueryExpansionCarriesHnQueries -v
```

Expected: `ImportError: cannot import name '_attach_hn_queries' from 'discovery.llm.stations.query_expansion'`.

- [ ] **Step 3: Implement the helper + wire it in**

Add to top imports of `src/discovery/llm/stations/query_expansion.py`:

```python
from discovery.llm.schemas import HackerNewsKeywordSpec
```

(Extend the existing line if it's `from discovery.llm.schemas import JobPlan, RedditQuerySpec, SubredditSearchPhrases` — add `HackerNewsKeywordSpec` to the import list.)

Then append `_attach_hn_queries` at the END of the module (after the existing `_drop_invalid_queries`):

```python
def _attach_hn_queries(
    plan: JobPlan, hn_queries: list[HackerNewsKeywordSpec]
) -> JobPlan:
    """Single point that re-attaches `hn_queries` to a post-tail plan.

    The locked Reddit tail (`_ground_selection`, `_force_time_window`,
    `_merge_baseline_subreddits`, `_drop_invalid_queries`) uses
    `JobPlan.model_construct(reddit_queries=..., reddit_subreddits=...)`
    at four sites and silently drops any non-Reddit fields. This helper
    is the carry-through: capture `hn_queries` once at the top of
    `run_query_expansion` (right after `_select_and_design`), let the
    locked tail run untouched, then call this helper exactly once to
    restore them before caching.

    Uses `model_construct` (skips validation) so the post-pruning
    Reddit fields -- which may be below the 25-30 band after
    `_drop_invalid_queries` -- still survive. The "too few survived"
    case is already enforced inside `_finalize`.

    See `docs/specs/2026-05-20-hackernews-source-design.md` §6.
    """
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=plan.reddit_subreddits,
        hn_queries=hn_queries,
    )
```

Wire it into `run_query_expansion` — change exactly two lines:

```python
async def run_query_expansion(spec: JobSpec) -> JobPlan:
    """Return a grounded `JobPlan` for `spec`. See module docstring.

    Raises `QueryExpansionError` on any failure in the chain; the caller
    (`plan_job`) catches it and falls back to the deterministic
    template.
    """
    key = cache_key(
        spec=spec.model_dump(mode="json"),
        prompt_version=f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}",
        model=MODEL,
    )
    cached = get_cached(_cache, key, JobPlan)
    if cached is not None:
        logger.debug("query_expansion cache hit for {}", key[:12])
        return cached

    logger.info("query_expansion cache miss; running grounded discovery")
    phrases = await _generate_phrases(spec)
    candidates = await _discover_subreddits(phrases)
    raw_plan = await _select_and_design(spec, candidates)

    hn_queries = list(raw_plan.hn_queries)                    # NEW: capture once
    grounded = _ground_selection(raw_plan, candidates)
    final_plan = _finalize(grounded, spec)
    final_plan = _attach_hn_queries(final_plan, hn_queries)   # NEW: restore once
    put_cached(_cache, key, final_plan)
    return final_plan
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/llm/stations/test_query_expansion.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green. The existing v5 tests in this file continue to pass (they emit `hn_queries=[]` by default, so the new attach is a no-op). The new tests pass too.

- [ ] **Step 5: Commit**

```
git add src/discovery/llm/stations/query_expansion.py tests/unit/llm/stations/test_query_expansion.py
git commit -m "feat(llm): carry hn_queries across the locked Wave-0 tail" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4.2: v6 prompt — Kind 3 HN section + master "What to emit" + build_user_message

**Files:**
- Modify: `src/discovery/llm/prompts/query_expansion.py` — bump `VERSION`, insert Kind 3 section, replace "# What to emit" body, append HN line to `build_user_message`.
- Modify: `tests/unit/llm/test_prompts_query_expansion.py` — add assertions for the v6-specific text + the `build_user_message` HN line.

**Spec reference:** §8 (full Kind 3 prompt text + master "What to emit" rewrite + `build_user_message` nudge).

**Cache invalidation note:** bumping `VERSION` v5 → v6 invalidates the combined Wave-0 cache key (`f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}"`). First runs after this task land cold — expected.

- [ ] **Step 1: Write the failing tests**

Extend imports in `tests/unit/llm/test_prompts_query_expansion.py` (the file should already import `query_expansion` as `qe` or similar — match the existing style):

```python
from discovery.llm.prompts.query_expansion import (
    SYSTEM_PROMPT,
    VERSION,
    build_user_message,
)
```

Add a new test class:

```python
class TestPromptV6Additions:
    """v6 = v5 plus the Kind 3 HN keyword candidate section and an
    updated master 'What to emit' that lists THREE fields. Spec §8.
    """

    def test_version_is_v6(self) -> None:
        assert VERSION == "v6"

    def test_kind_3_section_present(self) -> None:
        assert "Kind 3" in SYSTEM_PROMPT
        assert "Hacker News" in SYSTEM_PROMPT

    def test_hn_capability_framing_taught(self) -> None:
        # The prompt must explicitly tell the LLM that HN rewards
        # capability/launch framing, NOT pain framing.
        assert "CAPABILITY and LAUNCH framing" in SYSTEM_PROMPT

    def test_tag_redundancy_rule_present(self) -> None:
        # Issue 2 from the user's spec review -- the load-bearing
        # "don't waste tokens on Show HN" rule.
        assert "tag-redundant" in SYSTEM_PROMPT.lower() or 'Don\'t write\n   "Show HN"' in SYSTEM_PROMPT

    def test_first_two_positions_rule_present(self) -> None:
        # Rule 4: distinctive token must be in the first two positions
        # so decomposition keeps it.
        assert "first two positions" in SYSTEM_PROMPT

    def test_quality_over_quota_sparsity_clause_present(self) -> None:
        # Issue 3: non-tech industries get an explicit escape clause.
        assert "Quality over quota" in SYSTEM_PROMPT

    def test_strongest_first_ranking_signal_present(self) -> None:
        # Smaller issue 1: tell the LLM that emit order is a ranking signal.
        assert "STRONGEST CANDIDATES FIRST" in SYSTEM_PROMPT

    def test_python_does_not_enforce_ratio_clarifier_present(self) -> None:
        # §8 user-revision clarifier locking in Approach A intent.
        assert "Python does NOT enforce the ratio" in SYSTEM_PROMPT

    def test_master_what_to_emit_lists_three_fields(self) -> None:
        assert "JobPlan` with THREE fields" in SYSTEM_PROMPT
        # All three field names referenced in the master block.
        assert "reddit_queries" in SYSTEM_PROMPT
        assert "reddit_subreddits" in SYSTEM_PROMPT
        assert "hn_queries" in SYSTEM_PROMPT

    def test_build_user_message_includes_hn_nudge(self) -> None:
        from discovery.jobs import JobSpec
        from discovery.sources.reddit_subreddits import SubredditCandidate

        spec = JobSpec(industry="x", as_of=date(2026, 5, 20), time_window="month")
        table = [
            SubredditCandidate(
                name="startups",
                subscribers=5000,
                active_user_count=120,
                subreddit_type="public",
                public_description="x",
            )
        ]
        msg = build_user_message(spec, table)

        assert "hn_queries" in msg
        assert "HackerNews keyword candidates" in msg
        assert "capability/launch framing" in msg.lower() or "capability/launch" in msg
```

(Both `from discovery.jobs import JobSpec` and `from discovery.sources.reddit_subreddits import SubredditCandidate` already exist at the top of `tests/unit/llm/test_prompts_query_expansion.py` — delete any in-method copies; do not duplicate.)

Also update the EXISTING `test_version_is_v5` assertion in this file:

```python
# tests/unit/llm/test_prompts_query_expansion.py — find:
def test_version_is_v5(self) -> None:
    assert qe.VERSION == "v5"

# Replace with:
def test_version_is_v6(self) -> None:
    assert qe.VERSION == "v6"
```

This currently-passing assertion would otherwise turn red the moment Edit 1 in Step 3 bumps `VERSION` — keep the assertion in lockstep with the bump.

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/llm/test_prompts_query_expansion.py::TestPromptV6Additions -v
```

Expected: `AssertionError: assert 'v6' == 'v5'` on the first test (VERSION still says "v5").

- [ ] **Step 3: Apply the v6 prompt changes**

Three precise edits to `src/discovery/llm/prompts/query_expansion.py`. Apply each in order.

**Edit 1 — bump VERSION:**

Find:
```python
VERSION: str = "v5"
```

Replace with:
```python
VERSION: str = "v6"
```

Also extend the module docstring's version history (the docstring at the top of the file enumerates v1 through v5). Append a new entry after v5:

```
    v6 -- adds a third output (`hn_queries`) alongside the existing
    reddit fields. Introduces the "Kind 3 -- Hacker News keyword
    candidates" section teaching capability/launch framing,
    distinctive-token-in-first-two-positions, tag-redundancy
    avoidance, and graceful sparsity for non-tech industries.
    Master "What to emit" now lists THREE fields. Wave 0 cache
    invalidated automatically via the combined VERSION key.
```

**Edit 2 — insert the Kind 3 section after the v5 illustration's closing line.**

The v5 prompt closes its wedding-photography illustration with:

```
For any other industry these would be entirely different terms drawn
from THAT industry's real workflow. Re-derive; never copy.
```

After that line, insert the following block (this is spec §8 verbatim, ASCII-safe arrows and dashes):

```
# Kind 3 -- Hacker News keyword candidates (a SEPARATE output: hn_queries)

Hacker News is a flat site -- NO communities, NO subreddit equivalent.
Do not try to invent one. `hn_queries` is a separate, structurally
different output from `reddit_queries` / `reddit_subreddits`.

HN rewards CAPABILITY and LAUNCH framing, NOT pain framing. The
phrases that work on Reddit return zero or near-zero on HN. Phrases
that work on HN sound like:

- Capability claims:               "X for Y", "open-source X",
                                   "self-hosted X", "local-first X"
- Tech-stack qualifier:            "X in Rust", "Rust X",
                                   "WASM X", "Go X"

## Construction rules for HN keyword candidates

1. SHORT, DENSE PHRASES -- 2 to 4 words. Python will strip filler
   stopwords and keep only the FIRST 2 surviving content tokens, so
   think in PAIRS. Long phrases lose their tail tokens silently.

2. ACRONYMS ARE FIRST-CLASS. MCP, LLM, RAG, CLI, API, SSR, WASM,
   ETL, CRDT, gRPC, REST, OSS. HN's vocabulary is acronym-heavy and
   Python preserves casing during decomposition. Use acronyms where
   they're the natural HN term.

3. AVOID FILLER AND STOPWORDS. They get stripped in Python anyway;
   any phrase whose meaning DEPENDS on them ("the X of Y", "a way
   to", "how to") is wasted budget.

4. INDUSTRY-TERM + CAPABILITY/TECH-TERM COMBOS are the HN sweet
   spot -- BUT put the distinctive word in the first two positions
   so decomposition keeps it. Examples (every distinctive token
   survives): "local-first CRM", "Rust vector-db", "TypeScript
   agents", "scheduling CLI", "billing CRDT". Bury "CRM" or
   "framework" or "database" at position 3 and Python silently
   drops the very word that makes the phrase HN-suitable.

5. NO Reddit-flavored pain phrasings. "I would pay", "frustrated
   with", "wish there was", "tired of" -- these all return zero or
   near-zero on HN. They live in `reddit_queries`, not `hn_queries`.

6. DO NOT spend content tokens on tag-redundant words. Don't write
   "Show HN", "HN", "Ask HN" inside the keyword -- `intent=launch`
   already routes to `tags=show_hn` and `intent=context` to
   `tags=story` server-side. Putting those words in the keyword
   burns both content slots on the tag filter (the LLM's most
   common HN failure mode). Spend both content tokens on the
   substantive industry/capability terms.

## Tag each candidate's INTENT -- launch or context

For every HN candidate you emit, mark `intent`:

- launch -- phrase shaped to match a fresh "Show HN" launch (product
  name shape, "X for Y", new-thing framing). Python fires these
  against the date-sorted endpoint with relaxed quality filters so
  brand-new launches with low points still surface.
- context -- phrase shaped to match technical-discussion stories
  (debates, comparisons, deep-dives). Python fires these against
  the relevance-sorted endpoint with a server-side karma + comments
  floor.

AIM FOR ROUGHLY TWO-THIRDS LAUNCH AND ONE-THIRD CONTEXT (e.g. 6
launch + 3 context, or 8 launch + 4 context). The rationale tag
drives the routing per candidate; the 2:1 ratio is a target, not a
quota -- Python does NOT enforce the ratio, it routes each candidate
strictly by its own `intent` tag.

## What to emit for HN

Emit 8-15 `HackerNewsKeywordSpec` objects in `hn_queries` -- BUT if
the industry has weak HN coverage (trades, local services, non-
technical verticals), emit FEWER or ZERO candidates rather than
inventing tech-framed phrases. Quality over quota; downstream is
fine with an empty list. Each candidate has:

- `keyword`   -- the raw phrase, 2-4 words, casing preserved.
- `intent`    -- `launch` or `context`.
- `rationale` -- one short sentence: what HN content this should
                 surface and why it's HN-suitable.

EMIT YOUR STRONGEST CANDIDATES FIRST. Python caps the fired set at
6 in your emitted order, so ordering is a ranking signal -- your
best candidates must appear in the first ~6 positions.

Python downstream will decompose each keyword (drop stopwords, keep
<=2 content tokens, preserve casing), dedupe, route by `intent`,
build server-side `numericFilters` from the job's time window
(relaxed for launch queries), and cap the total at ~6 actually fired
against the API. Emit MORE than 6 candidates so the post-decomposition
survivors still cover both intents.

## HN illustration -- ONE example industry only (do NOT reuse these)

For the example industry "personal CRM for solo founders" (an HN-
native vertical chosen because it shows the pattern cleanly). Note
how every example puts the distinctive token in the FIRST TWO
positions so decomposition keeps it:

- "local-first CRM" (launch) -- local-first sub-trend launches.
- "CRM CLI" (launch) -- terminal-first product launches.
- "OSS CRM" (launch) -- open-source CRM launches.
- "SQLite CRM" (launch) -- SQLite-backed launch pattern.
- "CRM founder" (context) -- discussion of how founders organize
  relationship work.
- "contact privacy" (context) -- privacy-debate angle on contact
  storage.

For ANY OTHER industry you must RE-DERIVE different industry-specific
HN-shaped angles. Do not bolt this CRM vocabulary onto another
industry the way you must not reuse the wedding-photography
illustration above.
```

**Edit 3 — rewrite the master `# What to emit` section.**

Find the existing `# What to emit` block in the v5 prompt:

```
# What to emit

You will emit a JSON object validated as `JobPlan` with two fields:

- `reddit_queries` — between 25 and 30 `RedditQuerySpec` objects.
  Each has `endpoint`, `q`, `subreddit` (set for per_sub only),
  `sort`, `t`, `limit`, and a one-sentence `rationale` explaining
  why this query is worth running.
- `reddit_subreddits` — your shortlist of domain-relevant subreddits
  (without the `r/` prefix). Up to ~12. These complement the queries
  themselves; Python code may use this list to seed per-sub queries
  or rank subs for follow-up.

Each `rationale` is mandatory and visible to the engineer reviewing
plans. Be concrete: "scopes to nurse community for willingness-to-pay
signals on documentation tools" beats "looking for pain".
```

Replace with:

```
# What to emit

You will emit a JSON object validated as `JobPlan` with THREE fields:

- `reddit_queries` — between 25 and 30 `RedditQuerySpec` objects.
  Each has `endpoint`, `q`, `subreddit` (set for per_sub only),
  `sort`, `t`, `limit`, and a one-sentence `rationale` explaining
  why this query is worth running.
- `reddit_subreddits` — your shortlist of domain-relevant subreddits
  (without the `r/` prefix). Up to ~12. These complement the queries
  themselves; Python code may use this list to seed per-sub queries
  or rank subs for follow-up.
- `hn_queries` — 8-15 `HackerNewsKeywordSpec` objects (see "Kind 3 --
  Hacker News keyword candidates" above). Re-derive HN-shaped angles
  for THIS industry; do NOT translate the reddit_queries to HN.

Each `rationale` is mandatory and visible to the engineer reviewing
plans. Be concrete: "scopes to nurse community for willingness-to-pay
signals on documentation tools" beats "looking for pain".
```

**Edit 4 — extend `build_user_message` with the HN nudge.**

Find the existing closing `lines.append(...)` in `build_user_message`:

```python
    lines.append(
        "Produce a JobPlan with 25-30 reddit_queries using ONLY the "
        "subreddits above — a substantial share STANDARD pain-grid and a "
        "substantial share INDUSTRY-SPECIFIC (re-derived for THIS "
        "industry, not the prompt's wedding-photography illustration). "
        "Follow the system-prompt rules; explain each query's rationale."
    )
    return "\n".join(lines)
```

Replace with:

```python
    lines.append(
        "Produce a JobPlan with 25-30 reddit_queries using ONLY the "
        "subreddits above — a substantial share STANDARD pain-grid and a "
        "substantial share INDUSTRY-SPECIFIC (re-derived for THIS "
        "industry, not the prompt's wedding-photography illustration). "
        "Follow the system-prompt rules; explain each query's rationale."
    )
    lines.append("")
    lines.append(
        "Plus 8-15 hn_queries: HackerNews keyword candidates re-derived "
        "for THIS industry (capability/launch framing, NOT pain phrasing). "
        "Tag intent per candidate; aim ~2/3 launch / 1/3 context."
    )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green. The new `TestPromptV6Additions` passes; existing v5 prompt tests still pass (the v5 content is preserved verbatim).

If any existing test snapshots the entire SYSTEM_PROMPT or `build_user_message` output and asserts an exact length / hash, update it to the v6 baseline — that's a one-line snapshot refresh, not a behavior change.

- [ ] **Step 5: Commit**

```
git add src/discovery/llm/prompts/query_expansion.py tests/unit/llm/test_prompts_query_expansion.py
git commit -m "feat(llm): query_expansion v6 prompt — Kind 3 HN keyword candidates" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

**End of Chunk 4.** Wave 0 now produces a `JobPlan` with three typed fields. The combined Wave-0 cache key was invalidated by the v5 → v6 bump, so the next real-LLM run is cold (expected). The locked Reddit tail is untouched; `hn_queries` flows around it via the single capture-and-restore. Chunk 5 wires the worker registry, the parallel CLI fan-out, and the new project skill so the LLM-produced `hn_queries` actually reach the HackerNews adapter and into Bronze.

---

## Chunk 5: parallel fan-out + registry + new skill + handoff

Five tasks that wire the HN code into the runtime, lay down the project policy that locks in the deliberate divergences, and update the session-handoff log. After this chunk lands the slice is complete: `discovery run --industry X --location Y` enqueues one Reddit task + one HN task per job and dispatches them concurrently. Use @superpowers:test-driven-development on the three code tasks (5.1–5.3); 5.4 and 5.5 are doc-only with simplified steps.

### Task 5.1: `claim_known_task` additive worker primitive

**Files:**
- Modify: `src/discovery/workers/worker.py` — add `claim_known_task` alongside the existing `claim_one`. Do NOT modify `claim_one` or `run_one`.
- Modify: `src/discovery/workers/__init__.py` — add `claim_known_task` to the imports and `__all__`.
- Modify: `tests/unit/test_worker.py` (or wherever the existing `claim_one` tests live — glob `tests/unit/test_worker*.py` to find the file) — add `TestClaimKnownTask` class.

**Spec reference:** §12 (parallel fan-out — `claim_known_task` is the additive race-safe per-id claim that routes around the documented single-worker-safe `claim_one`).

- [ ] **Step 1: Write the failing tests**

First glob to find the existing worker test file:

```
grep -l "def test.*claim_one" tests/
```

Then in that file (typically `tests/unit/test_worker.py`), add:

```python
import asyncio  # add to top imports if missing

from discovery.workers.worker import claim_known_task  # extend existing worker import line


class TestClaimKnownTask:
    """The additive per-id claim that routes around `claim_one`'s
    documented single-worker race. Mirror file-test patterns from
    `TestClaimOne`."""

    async def test_claims_queued_task_atomically(self, session: AsyncSession) -> None:
        """Happy path: claims a queued task, flipping status to running,
        stamping claimed_at, and incrementing attempts."""
        # Build a job + queued task. Use the file's existing helpers
        # if it has them (e.g. `_make_job`, `_make_task`); otherwise
        # construct inline mirroring TestClaimOne.
        job = Job(spec={"industry": "x", "as_of": "2026-05-20", "time_window": "month"}, spec_hash="h")
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = Task(
            job_id=job.id,
            wave=1,
            source="reddit",
            action="fetch",
            params={"queries": []},
            content_hash="taskhash",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        claimed = await claim_known_task(session, task.id)

        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status == TaskStatus.running
        assert claimed.claimed_at is not None
        assert claimed.attempts == 1

    async def test_returns_none_when_task_already_running(
        self, session: AsyncSession
    ) -> None:
        """Race-safety contract: the WHERE clause includes status=queued
        so a second claim returns None instead of double-running."""
        job = Job(spec={"industry": "x", "as_of": "2026-05-20", "time_window": "month"}, spec_hash="h")
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = Task(
            job_id=job.id, wave=1, source="reddit", action="fetch",
            params={"queries": []}, content_hash="taskhash2",
            status=TaskStatus.running,  # already claimed by someone else
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        result = await claim_known_task(session, task.id)
        assert result is None

    async def test_returns_none_for_nonexistent_task_id(
        self, session: AsyncSession
    ) -> None:
        result = await claim_known_task(session, 999_999)
        assert result is None

    async def test_double_claim_on_same_id_returns_none_second_time(
        self, session: AsyncSession
    ) -> None:
        """Sequential demonstration of the contract: first call wins,
        second sees status='running' and returns None."""
        job = Job(spec={"industry": "x", "as_of": "2026-05-20", "time_window": "month"}, spec_hash="h")
        session.add(job)
        await session.commit()
        await session.refresh(job)

        task = Task(
            job_id=job.id, wave=1, source="reddit", action="fetch",
            params={"queries": []}, content_hash="taskhash3",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        first = await claim_known_task(session, task.id)
        second = await claim_known_task(session, task.id)

        assert first is not None
        assert second is None
```

If the existing worker test file doesn't have a `session` fixture, copy the same 9-line fixture from `tests/unit/test_orchestrator_reddit.py:28-36` (the per-file convention). Add `Job` and `TaskStatus` to imports if they're not already there.

- [ ] **Step 2: Run; expect failure**

```
uv run pytest -k "TestClaimKnownTask" -v
```

Expected: `ImportError: cannot import name 'claim_known_task' from 'discovery.workers.worker'`.

- [ ] **Step 3: Implement `claim_known_task`**

Add to `src/discovery/workers/worker.py`. New top-of-file import:

```python
from sqlalchemy import update as sa_update
```

Then add the function (after `claim_one`, before `run_one`):

```python
async def claim_known_task(session: AsyncSession, task_id: int) -> Task | None:
    """Atomically flip a SPECIFIC task from queued to running.

    Race-safe per-id claim: the UPDATE's WHERE clause includes
    `status='queued'`, so SQLite's per-transaction lock serializes
    concurrent callers -- at most one sees the row as queued and the
    others' UPDATE matches zero rows.

    This is the additive per-id analog of `claim_one`. `claim_one` is
    documented single-worker-safe only (SELECT-then-UPDATE pair); this
    helper is the minimum addition that lets `cli/run.py`'s parallel
    fan-out claim two known task ids concurrently without lifting the
    single-worker assumption from CLAUDE.md.

    Returns the claimed Task on success, None if the task was already
    claimed / not queued / nonexistent.

    See `docs/specs/2026-05-20-hackernews-source-design.md` §12.
    """
    now = datetime.now(UTC)
    stmt = (
        sa_update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.queued)
        .values(
            status=TaskStatus.running,
            claimed_at=now,
            attempts=Task.attempts + 1,
        )
    )
    result = await session.exec(stmt)
    if result.rowcount == 0:
        # Nothing matched -- task is no longer queued (already running /
        # done / nonexistent). Roll back any session-level state and
        # return None.
        await session.rollback()
        return None
    await session.commit()
    return await session.get(Task, task_id)
```

Then export it from `src/discovery/workers/__init__.py`. Update the import line and `__all__`:

```python
from discovery.workers.worker import (
    SourceRegistry,
    aclose_registry,
    claim_known_task,        # NEW
    claim_one,
    run_one,
    run_worker_drain,
    run_worker_once,
    sweep_stuck_tasks,
)
```

And add `"claim_known_task",` to `__all__` (alphabetically next to `claim_one`).

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest -k "TestClaimKnownTask" -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green. Existing worker tests untouched. New 4 tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/workers/worker.py src/discovery/workers/__init__.py tests/unit/test_worker.py
git commit -m "feat(workers): claim_known_task race-safe per-id task claim" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5.2: `cli/run.py` parallel fan-out via `asyncio.gather`

**Files:**
- Modify: `src/discovery/cli/run.py` — split `_run_discovery` into setup/dispatch/report phases; replace the sequential drain with `asyncio.gather` over two `_run_task_in_own_session` calls.
- Test: `tests/unit/test_run_cli_parallel.py` (new file) OR extend an existing `tests/unit/test_smoke.py` / cli test file. Glob first.

**Spec reference:** §12.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_run_cli_parallel.py`:

```python
"""Parallel fan-out tests for `discovery.cli.run._run_task_in_own_session`.

The discovery run command enqueues a Reddit task and an HN task per
job, then dispatches both concurrently via `asyncio.gather`. This file
tests the per-id-claim + dispatch helper directly (faster + clearer
than booting the whole CLI) and pins the wall-clock overlap that
proves concurrency.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlmodel import SQLModel

from discovery.cli.run import _run_task_in_own_session
from discovery.db import models  # noqa: F401 -- registers tables on metadata
from discovery.db.engine import async_session_factory, create_engine_for
from discovery.db.models import Job, Task, TaskStatus
from discovery.sources.base import BaseSource, RawRecord


class _RecordingBase(BaseSource):
    """Test double that records its start time and sleeps a known
    interval. Used to prove that two adapters run with overlapping
    wall-clock windows when dispatched concurrently.

    `BaseSource.__init_subclass__` requires `name` to be a CLASS
    attribute (not just an instance attribute / annotation), so the
    two concrete doubles below set `name` at class scope. Setting
    `self.name = ...` in `__init__` would NOT satisfy the check --
    the check runs at class creation, before `__init__` is called.
    """

    name = "recording"  # subclasses override; required so __init_subclass__ check passes
    rate_limit = (10, 1)

    def __init__(self, started_at: dict[str, float], sleep_s: float) -> None:
        self._started_at = started_at
        self._sleep_s = sleep_s

    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        self._started_at[self.name] = time.monotonic()
        await asyncio.sleep(self._sleep_s)
        return [RawRecord(source=self.name, external_id=f"{self.name}-1", body={})]


class _RedditDouble(_RecordingBase):
    name = "reddit"


class _HNDouble(_RecordingBase):
    name = "hackernews"


@pytest.fixture
async def maker() -> AsyncIterator[Any]:
    engine = create_engine_for("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_session_factory(engine)
    await engine.dispose()


async def _make_queued_task(maker: Any, source: str) -> int:
    """Insert a job + queued task; return the task id."""
    async with maker() as s:
        job = Job(
            spec={"industry": "x", "as_of": "2026-05-20", "time_window": "month"},
            spec_hash=f"h-{source}",
        )
        s.add(job)
        await s.commit()
        await s.refresh(job)

        task = Task(
            job_id=job.id, wave=1, source=source, action="fetch",
            params={"queries": []}, content_hash=f"hash-{source}",
        )
        s.add(task)
        await s.commit()
        await s.refresh(task)
        return task.id  # type: ignore[return-value]


class TestRunTaskInOwnSession:
    async def test_claims_and_dispatches_known_task(self, maker: Any) -> None:
        task_id = await _make_queued_task(maker, "reddit")
        started: dict[str, float] = {}
        registry: dict[str, BaseSource] = {"reddit": _RedditDouble(started, 0.0)}

        await _run_task_in_own_session(maker, registry, task_id)

        # Task is now done.
        async with maker() as s:
            task = await s.get(Task, task_id)
            assert task is not None
            assert task.status == TaskStatus.done
        assert started["reddit"]  # adapter was invoked

    async def test_returns_silently_when_task_already_claimed(
        self, maker: Any
    ) -> None:
        """If claim_known_task returns None (task no longer queued),
        the helper logs a warning and returns -- it does NOT raise."""
        task_id = await _make_queued_task(maker, "reddit")
        # Pre-claim it manually so the helper sees it as already running.
        async with maker() as s:
            task = await s.get(Task, task_id)
            assert task is not None
            task.status = TaskStatus.running
            await s.commit()

        registry: dict[str, BaseSource] = {}
        # Should NOT raise; adapter is never called.
        await _run_task_in_own_session(maker, registry, task_id)


class TestParallelFanout:
    async def test_two_tasks_dispatch_concurrently(self, maker: Any) -> None:
        """Wall-clock overlap proves the two branches actually run in
        parallel via asyncio.gather -- not sequentially."""
        reddit_id = await _make_queued_task(maker, "reddit")
        hn_id = await _make_queued_task(maker, "hackernews")

        started: dict[str, float] = {}
        sleep_s = 0.05  # 50 ms each
        registry: dict[str, BaseSource] = {
            "reddit": _RedditDouble(started, sleep_s),
            "hackernews": _HNDouble(started, sleep_s),
        }

        t0 = time.monotonic()
        await asyncio.gather(
            _run_task_in_own_session(maker, registry, reddit_id),
            _run_task_in_own_session(maker, registry, hn_id),
        )
        wall = time.monotonic() - t0

        # Both started within 30ms of each other -- overlapped. Bound
        # is generous for Windows CI's ~15ms timer granularity; the
        # spirit of the assertion is "near-simultaneous", not "≤20ms".
        assert len(started) == 2
        assert abs(started["reddit"] - started["hackernews"]) < 0.03

        # Total wall-clock is closer to 50ms (max) than 100ms (sum).
        # 90ms upper bound: well under the 100ms sequential floor with
        # headroom for OS scheduling jitter. If this flakes on a slow
        # runner, bump higher -- the load-bearing claim is "<100ms",
        # not "exactly ~50ms".
        assert wall < 0.09, f"wall={wall:.3f}s -- looks sequential"
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest tests/unit/test_run_cli_parallel.py -v
```

Expected: `ImportError: cannot import name '_run_task_in_own_session' from 'discovery.cli.run'`.

- [ ] **Step 3: Refactor `cli/run.py` into 3 phases + the helper**

Update imports at the top of `src/discovery/cli/run.py`:

```python
import asyncio  # already imported
from datetime import date
from typing import Any  # new (for the maker type)

from loguru import logger  # new (for the not-claimed warning)
import typer
from rich.console import Console

from discovery.cli.inspect import render_job_detail
from discovery.db.engine import async_session_factory, get_engine
from discovery.jobs import JobSpec, create_job
from discovery.orchestrator.hackernews import enqueue_hn_task_for_job  # NEW
from discovery.orchestrator.jobs import plan_job
from discovery.orchestrator.reddit import enqueue_reddit_task_for_job
from discovery.view import gather_job_detail
from discovery.workers import (
    SourceRegistry,
    aclose_registry,
    build_default_registry,
    claim_known_task,  # NEW
    run_one,           # NEW (was implicitly used via run_worker_once)
)
```

(Drop the existing `run_worker_once` import — the new flow goes through `claim_known_task` + `run_one` directly.)

Rewrite `_run_discovery` into three phases. Replace the entire function:

```python
async def _run_discovery(
    industry: str,
    location: str | None,
    size: str | None,
    as_of: date,
    time_window: str,
) -> None:
    engine = get_engine()
    maker = async_session_factory(engine)
    registry = build_default_registry()

    spec = JobSpec(
        industry=industry,
        location=location,
        size=size,
        as_of=as_of,
        time_window=time_window,  # type: ignore[arg-type]
    )

    try:
        # Phase 1: create the job, run Wave 0 inline, enqueue BOTH
        # source tasks in one session block. Capture ids for Phase 2.
        async with maker() as session:
            job = await create_job(session, spec)
            console.print(
                f"[bold]job:[/bold] {job.id}  "
                f"[dim](spec_hash {job.spec_hash[:12]}…, "
                f"status {job.status.value})[/dim]"
            )

            # Wave 0: LLM query expansion via OpenAI gpt-5.4. On
            # success this populates job.job_plan with three fields
            # (reddit_queries, reddit_subreddits, hn_queries); on
            # failure (no API key, LLM error, validation drops too
            # many queries) job.job_plan stays null and BOTH
            # orchestrators fall back to their deterministic templates.
            job = await plan_job(session, job)
            plan_status = "planned" if job.job_plan is not None else "fallback"
            console.print(f"[bold]wave 0:[/bold] {plan_status}")

            reddit_task = await enqueue_reddit_task_for_job(session, job)
            hn_task = await enqueue_hn_task_for_job(session, job)
            console.print(
                f"[bold]queued tasks:[/bold] "
                f"reddit={reddit_task.id} "
                f"(queries={len(reddit_task.params['queries'])}), "
                f"hackernews={hn_task.id} "
                f"(queries={len(hn_task.params['queries'])})"
            )

            job_id = job.id
            reddit_task_id = reddit_task.id
            hn_task_id = hn_task.id

        # Phase 2: parallel dispatch by known task id. Each branch
        # opens its own session -- AsyncSession is not safe to share
        # across concurrent ops, and `claim_known_task` is race-safe
        # per-id (it routes around the single-worker-safe `claim_one`).
        # `run_one` already catches and finalizes adapter failures
        # internally, so partial success across sources is automatic:
        # if Reddit fails entirely and HN succeeds, the job still
        # produces HN raw_records (and vice versa).
        console.print("[bold]running reddit + hackernews concurrently...[/bold]")
        assert reddit_task_id is not None
        assert hn_task_id is not None
        await asyncio.gather(
            _run_task_in_own_session(maker, registry, reddit_task_id),
            _run_task_in_own_session(maker, registry, hn_task_id),
        )
        console.print("[bold green]done.[/bold green] 2 task(s) processed.")

        # Phase 3: report. Fresh session for the read-only detail
        # gather (the Phase-1 session was closed after enqueue).
        if job_id is not None:
            async with maker() as session:
                detail = await gather_job_detail(session, job_id=job_id, post_limit=5)
                if detail is not None:
                    console.print()
                    render_job_detail(detail)
    finally:
        await aclose_registry(registry)
        await engine.dispose()


async def _run_task_in_own_session(
    maker: Any,
    registry: SourceRegistry,
    task_id: int,
) -> None:
    """Open a fresh session, atomically claim the task by id, dispatch
    it. One session per concurrent branch (AsyncSession is not
    safe to share across concurrent ops). If the task is no longer
    queued by the time we get to it (very unlikely under
    single-worker, but defensive), log and return -- do not raise.
    """
    async with maker() as s:
        task = await claim_known_task(s, task_id)
        if task is None:
            logger.warning("task {} not claimed (already running/done?)", task_id)
            return
        await run_one(s, registry, task)
```

(Leave `run_command` — the typer-decorated CLI entry — unchanged. Only `_run_discovery` and the new `_run_task_in_own_session` change.)

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest tests/unit/test_run_cli_parallel.py -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green. If any existing CLI test fails because it asserted on the old "processed N tasks" log line, update it to the new "queued tasks: reddit=... hackernews=..." shape or assert on the underlying DB state (more durable than log strings).

- [ ] **Step 5: Commit**

```
git add src/discovery/cli/run.py tests/unit/test_run_cli_parallel.py
git commit -m "feat(cli): parallel Reddit+HN fan-out via asyncio.gather + claim_known_task" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5.3: Register `HackerNewsSource` in `build_default_registry`

**Files:**
- Modify: `src/discovery/workers/__init__.py` — add the `HackerNewsSource` registration line.
- Modify: existing registry tests if any (glob `tests/unit/test_workers*.py` or `tests/unit/workers/`). If none assert on registry contents, add a small new test.

**Spec reference:** §13.

- [ ] **Step 1: Write the failing tests**

Find the existing registry tests (likely `tests/unit/workers/test_init.py` or `tests/unit/test_workers.py`). If none, create `tests/unit/workers/test_registry.py`:

```python
from __future__ import annotations

from discovery.sources.hackernews import HackerNewsSource
from discovery.sources.reddit import RedditSource
from discovery.workers import build_default_registry


class TestBuildDefaultRegistry:
    def test_includes_reddit_adapter(self) -> None:
        registry = build_default_registry()
        assert "reddit" in registry
        assert isinstance(registry["reddit"], RedditSource)

    def test_includes_hackernews_adapter(self) -> None:
        registry = build_default_registry()
        assert "hackernews" in registry
        assert isinstance(registry["hackernews"], HackerNewsSource)

    def test_hackernews_adapter_constructed_without_credentials(self) -> None:
        """HN needs no creds; the registration line must construct
        HackerNewsSource() with no kwargs."""
        registry = build_default_registry()
        hn = registry["hackernews"]
        # Confirms aclose() is callable -- the adapter owns its client.
        assert hasattr(hn, "aclose")
```

- [ ] **Step 2: Run; expect failure**

```
uv run pytest -k "TestBuildDefaultRegistry" -v
```

Expected: `KeyError: 'hackernews'` (registry doesn't include it yet) OR `assert "hackernews" in registry` fails.

- [ ] **Step 3: Add the registration**

In `src/discovery/workers/__init__.py`, update `build_default_registry`:

```python
def build_default_registry() -> SourceRegistry:
    """Production registry. Reads source credentials/UA strings from settings.

    Add new adapters here as they land. Each adapter is constructed once
    per worker process and reused for every task that targets it.
    """
    from discovery.config.settings import settings  # noqa: PLC0415 — lazy on purpose
    from discovery.sources.hackernews import HackerNewsSource  # noqa: PLC0415  # NEW
    from discovery.sources.reddit import RedditSource  # noqa: PLC0415

    adapters: dict[str, BaseSource] = {
        "reddit": RedditSource(user_agent=settings.reddit_user_agent),
        "hackernews": HackerNewsSource(),  # NEW -- no auth, no UA
    }
    return adapters
```

`aclose_registry` already iterates and calls `aclose()` on every adapter; `HackerNewsSource.aclose()` closes its owned client. No change needed there.

- [ ] **Step 4: Run tests + project checks; expect pass**

```
uv run pytest -v
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
```

All green; 3 new tests pass.

- [ ] **Step 5: Commit**

```
git add src/discovery/workers/__init__.py tests/unit/workers/test_registry.py
git commit -m "feat(workers): register HackerNewsSource in default registry" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5.4: Create `.claude/skills/hackernews-source/SKILL.md`

**Files:**
- Create: `.claude/skills/hackernews-source/SKILL.md`

**Authorization note:** CLAUDE.md's "Don't touch without asking" list includes `.claude/`. The owner explicitly authorized this skill file in the HN guide message (this guide becomes the project policy here). No other `.claude/` changes are in scope.

**Spec reference:** §14 (skill outline + the explicitly documented divergences).

This task does NOT follow the TDD pattern — it's a pure documentation deliverable. Three steps: write the file, verify project checks stay green, commit.

- [ ] **Step 1: Create the skill file**

Create `.claude/skills/hackernews-source/SKILL.md`:

```markdown
# HackerNews Source Adapter — Operational Playbook

This file is the project's policy on HackerNews. Read it end-to-end
before writing or modifying `src/discovery/sources/hackernews.py`,
`src/discovery/orchestrator/hackernews.py`, or planning HN queries
from a `JobPlan`. The numbered items below are cross-referenced by
number in commits and reviews — don't renumber them.

The `source-adapter` skill is the umbrella contract (async, rate-
limited, retried, Pydantic-validated, idempotent, stored verbatim).
This file is the HN-specific layer on top — and where HN deliberately
diverges, it says so.

The companion design doc is
`docs/specs/2026-05-20-hackernews-source-design.md`.

---

## 1. Use the Algolia HN Search API. Don't scrape news.ycombinator.com.

Hacker News has an official search backend hosted by Algolia. It's
free, needs no API key, no auth, no User-Agent requirement, and is
generous on rate limits (~10,000 requests/hour — effectively
unlimited for a scanner).

Base URLs:
- `https://hn.algolia.com/api/v1/search` — relevance-ranked
- `https://hn.algolia.com/api/v1/search_by_date` — reverse-chronological

Never parse HTML from `news.ycombinator.com`. The Algolia API returns
clean JSON with `points`, `num_comments`, `author`, `created_at`,
`objectID`, and `_tags` already structured.

## 2. Two endpoints, two purposes — transport flag at plan time.

This is the single most important design decision in the adapter.

- `/search` ranks by relevance (points + text match + freshness). Use
  for broad topic/discussion queries (`intent=context`).
- `/search_by_date` is strict reverse-chronological. Use for fresh
  product launches that haven't accumulated points yet
  (`intent=launch`).

Project queries carry a transport flag `endpoint: "search" |
"search_by_date"`. `build_search_url` prepends the base URL at fetch
time.

## 3. Tags taxonomy + AND/OR semantics.

Algolia HN tags: `story`, `comment`, `ask_hn`, `show_hn`, `poll`,
`front_page`, `author_{username}`, `story_{id}`. Comma between tag
values = AND; parentheses = OR.

- Project uses `tags=show_hn` for launch queries.
- Project uses `tags=story` for context queries.

Empirically verified (2026-05-20) that Ask HN and Show HN posts
carry BOTH `story` AND their subtype tag in `_tags`, so `tags=story`
is a true superset that catches Ask HN's pain-shaped "how do you
handle X?" threads — the closest HN gets to Reddit-style problem
discussion. No need to OR `(story,ask_hn)`.

## 4. Strict token-AND on the `query` parameter — no OR operator.

This is the #1 cause of "why am I getting zero HN results." Unlike
Reddit, you cannot OR phrases together. Every content token in the
`query` parameter must co-occur in the matched story.

Consequence: a 4+ word keyword like "privacy preserving data
collection library" demands all five words appear in a short HN
title. Almost never happens. Long keywords starve the source.

The fix: decompose every keyword to its first ~2 content tokens
before querying. See item 5.

## 5. Decomposition policy.

Pure helper `discovery.sources.keyword_tokens.decompose_keyword`:

1. Whitespace-split the keyword.
2. Drop tokens whose lowercased form is in the small stopword set
   (`a, an, the, for, with, to, of, in, on, and, or`).
3. Keep the first 2 surviving tokens.
4. Preserve ORIGINAL casing — HN's vocabulary is acronym-heavy
   (MCP, CLI, RAG, LLM, WASM, ETL, CRDT, OSS) and lowercasing them
   loses signal.
5. Return `[]` if nothing survives (caller drops the query).

Stopword set is deliberately small per the guide ("a big list starts
eating real content"). This is a CODE change with tests if it ever
needs tuning, not a runtime knob.

Reusable later for GitHub code search / arXiv (all token-AND), but
kept HN-only here without pre-generalization.

## 6. Server-side `numericFilters` IS the quality floor.

Algolia supports server-side `numericFilters` (comma=AND between
clauses). The project uses:

- `created_at_i>{epoch}` — recency floor from `JobSpec.time_window`
  (`_time_window_epoch` in the orchestrator computes the floor at
  midnight UTC; `all` → omit the filter).
- For context queries: ALSO `points>5,num_comments>3` — server-side
  quality floor.
- For launch queries: RELAXED — no points/comments floor. Fresh
  Show HN launches legitimately sit at 0–3 points for hours, and
  the recency is the signal.

Client-side `keep_hit` is a near-noop (only drops hits missing
`objectID`, which Algolia never actually omits). All quality work
happens server-side.

## 7. Cap total queries per task at ~6.

Even though the rate limit is generous, more queries = more downstream
LLM cost + duplicate noise, with diminishing returns. `MAX_HN_QUERIES
= 6` (in `orchestrator/hackernews.py`). The Wave 0 LLM emits 8–15
candidates; Python decomposes, dedupes, and truncates to the first 6
in the LLM's emitted order (a ranking signal).

## 8. Set `hitsPerPage=30` explicitly. No pagination.

Algolia's default `hitsPerPage` is small (~20). Set it to 30 so each
request returns enough candidates without paging. The top 30 by
relevance or date is what matters for a scanner — don't build paging
you won't use.

## 9. Per-instance limiter, NOT a process-wide singleton.

Reddit uses `reddit_ratelimit.py`'s shared singleton because TWO
consumers (Wave 0 sub-search + Wave 1 content fetch) must share ONE
10-req/min budget. HN has exactly ONE consumer (Wave 1 fetch — there
is no HN subreddit-discovery analog) and Algolia's ceiling is
~10k/hr (effectively unlimited).

Each `HackerNewsSource` instance gets a fresh `AsyncLimiter(5, 1)`
(5 req/s polite, far under Algolia's actual ceiling).

**Documented divergence from `reddit-source` skill.** Don't "fix"
this by adding an `hn_ratelimit.py` singleton — there's nothing for
it to coordinate.

## 10. No retry, partial success across queries.

The HN guide is emphatic: HN does NOT need Reddit's retry dance. No
429/Retry-After machinery, no exponential backoff, no skill-item-4
analog. One GET per query; non-2xx or `httpx.HTTPError` records that
query's error and the loop continues to the next.

Project-locked partial-success contract still applies: if `fetch`
batches ~6 queries and some succeed, return what worked; only when
EVERY query fails does `fetch` raise (the first error) so the worker
marks the task failed.

**Documented divergence from `source-adapter` umbrella.** The umbrella
says "wrap the network call with @tenacity.retry — exponential
backoff, max 3 attempts." HN deliberately does not. This is the
single largest deliberate divergence; don't add retry by stealth.

## 11. Bronze stores raw — Wave 2 parses.

`hit_to_raw_record` sets `external_id = str(hit["objectID"])` and
`body = hit` verbatim. No snippet construction, no permalink fallback,
no body trimming. The HN guide discusses these as adapter-side
concerns; in THIS project they are explicitly **deferred to Wave 2**
because the locked Bronze contract is "store raw."

When Wave 2 lands and needs the permalink for an Ask/Show HN text
post whose `url` is null, it constructs
`https://news.ycombinator.com/item?id={objectID}` from the verbatim
body.

## 12. HN signals are CAPABILITY, not pain. (Downstream / Wave 2.)

Tag the signals downstream as "capability/launch" — the opposite of
Reddit's "pain/adoption". The Wave 0 prompt teaches the LLM the
framing for keyword generation (capability/launch phrasings, NOT
pain phrasings). The adapter itself just stores raw; tagging is a
Wave 2 concern.

## 13. Things that will tempt you and shouldn't.

- Don't try to OR keywords into one big query. The API doesn't
  support it. Run separate queries (Python compiles the candidate
  list into ≤6 separate queries).
- Don't send long multi-word phrases. The decomposition cap is 2
  content tokens; longer phrases lose their tail tokens silently.
  This is the #1 silent-failure mode.
- Don't write "Show HN" / "HN" / "Ask HN" inside a keyword. Those
  are tag filters, not content. Putting them in the query burns
  both content slots on tag-redundant words (next-most-common
  failure mode). The v6 prompt explicitly forbids this.
- Don't lowercase or stem tokens. Acronyms are high-signal on HN
  and casing/exact-match matter.
- Don't use only `/search`. You'll systematically miss fresh
  launches — exactly the signal you most want for idea generation.
  Always include `/search_by_date` for the launch queries.
- Don't paginate. Top 30 by relevance/date is plenty.
- Don't filter NSFW or do heavy body cleaning. HN doesn't need it.
- Don't add retry logic by stealth. The "no retry" decision is
  load-bearing (item 10 above).
- Don't mirror Reddit's process-wide singleton limiter (item 9).

## 14. The mental model.

Reddit = where people complain (pain signals). HN = where people ship
and discuss tech (capability signals). They're complementary halves
of the same research question. Mix the two as separate `_tags`
downstream; don't dump them into one bucket.

For HN, optimize for:

1. Catching fresh launches — `show_hn` + `/search_by_date` with
   relaxed quality filters is the killer combo.
2. Query construction discipline — short token-AND queries, always.
   This is where 90% of HN bugs live.
3. Server-side engagement filtering — `points + num_comments` for
   context queries; relaxed for launches.

The API will rarely fight you. Your own query strings will.

---

## Divergences from related skills (single point of truth)

The HN adapter deliberately diverges from `source-adapter` (umbrella)
and from `reddit-source` (sister adapter) on three points:

- **No retry / backoff** (item 10). `source-adapter` umbrella mandates
  `@tenacity.retry` with exponential backoff. HN deliberately does
  not; project-locked partial-success across queries is preserved.
- **Per-instance limiter** (item 9). `reddit-source` uses the
  `reddit_ratelimit.py` process-wide singleton because two Reddit
  consumers share a budget. HN has one consumer and a generous
  ceiling.
- **HN guide's adapter-side normalization** (snippet, permalink
  fallback, missing-`url` handling, capability tagging) is **deferred
  to Wave 2** in this project (item 11). The HN guide describes these
  as adapter-side because it was written for a different downstream;
  here Bronze stores raw and Wave 2 owns parsing.

All three divergences are user-approved, surfaced in the spec
(`docs/specs/2026-05-20-hackernews-source-design.md` §16), and
documented inline above so future sessions don't "fix" them.
```

- [ ] **Step 2: Run project checks**

```
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/
uv run pytest
```

All green. (No code changes — this step is just confirming nothing in the .claude/skills/ path breaks tooling.)

- [ ] **Step 3: Commit**

```
git add .claude/skills/hackernews-source/SKILL.md
git commit -m "docs(skill): hackernews-source operational policy (14 items + divergences)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5.5: Update `docs/handoff.md`

**Files:**
- Modify: `docs/handoff.md` — add a new dated "what shipped" section at the top of the change history, update commit table, refresh the "What's NOT built yet" list, refresh the "Next slice: open" candidates.

**Spec reference:** the handoff IS the running session-handoff log; this is the final-mile update that future sessions read first.

Doc-only task. No tests; no TDD pattern.

- [ ] **Step 1: Read the current handoff structure**

```
Read docs/handoff.md
```

Identify the most recent "## … (YYYY-MM-DD) — what shipped & locked in" section. The new HN section goes immediately above it (newest first).

- [ ] **Step 2: Write the HN section + update the rolling lists**

Add a new section immediately above the "Wider query band + industry brainstorm (2026-05-16) — what shipped & locked in" section:

```markdown
## HackerNews source adapter (2026-05-20) — what shipped & locked in

Built from `docs/specs/2026-05-20-hackernews-source-design.md` (approved,
3-pass spec-reviewed, owner-revised: 8 prompt + template edits + 1
empirical Algolia tag check) via `docs/plans/2026-05-20-hackernews-source.md`
(5-chunk plan, each chunk reviewed). 14 commits across 5 chunks. Every
task: TDD red→green→commit; per-chunk plan-reviewer dispatch + fix loop.

**Problem solved:** Wave 1 only had Reddit. Bronze accumulated pain-
shaped signals but missed HN's complementary capability/launch
signals. The slice adds HN as a second source so every `discovery run`
fans out to Reddit AND HackerNews concurrently.

**New pieces (add to the map above):**

- `src/discovery/sources/keyword_tokens.py` — pure
  `decompose_keyword` (whitespace-split, drop stopwords, keep first
  2 surviving tokens, preserve casing). Reusable later by GitHub /
  arXiv (also token-AND APIs).
- `src/discovery/sources/hackernews.py` — `HackerNewsSource` with
  per-instance `AsyncLimiter(5, 1)` (NOT a singleton), no retry,
  partial-success across queries, owned `httpx.AsyncClient` closed
  via `aclose`. Pure helpers `build_search_url`, `keep_hit`,
  `hit_to_raw_record` (verbatim Bronze, no normalization).
- `src/discovery/orchestrator/hackernews.py` — `_time_window_epoch`,
  `_routing_for` (launch→show_hn+search_by_date+relaxed, context→
  story+search+points>5,num_comments>3), `_compile_hn_queries`
  (decompose→dedupe→route→numericFilters→≤6 cap, preserves LLM
  order), `hn_keyword_candidates_for_spec` (no-LLM template
  fallback, capability-first), `enqueue_hn_task_for_job`
  (idempotent on `content_hash`).
- `src/discovery/llm/schemas.py` — `HackerNewsKeywordSpec`
  (`keyword`, `intent: Literal["launch","context"]`, `rationale`,
  all frozen). `JobPlan.hn_queries: list[HackerNewsKeywordSpec] =
  Field(default_factory=list)` — permissive default (no `min_length`)
  is deliberate, prevents HN under-production from raising
  QueryExpansionError and sinking the Reddit grounded plan.
- `src/discovery/llm/stations/query_expansion.py` — `_attach_hn_queries`
  carry-through helper; 2-line wiring in `run_query_expansion`
  (capture once after `_select_and_design`, restore once after
  `_finalize`). The locked Reddit tail's 4 `model_construct` sites
  stay byte-for-byte untouched.
- `src/discovery/llm/prompts/query_expansion.py` — `VERSION` v5→v6;
  new Kind 3 section (capability/launch framing, distinctive-token-
  in-first-two-positions, tag-redundancy avoidance, graceful
  sparsity for non-tech industries, 2:1 launch:context routing
  signal). Combined Wave-0 cache invalidated automatically.
- `src/discovery/workers/worker.py` — additive `claim_known_task`
  (race-safe per-id claim via `UPDATE...WHERE id=? AND status=
  'queued'`). `claim_one` UNTOUCHED.
- `src/discovery/workers/__init__.py` — exports `claim_known_task`;
  `build_default_registry` registers `"hackernews": HackerNewsSource()`.
- `src/discovery/cli/run.py` — `_run_discovery` split into setup
  (job + plan + enqueue both) / parallel dispatch (`asyncio.gather`
  over two `_run_task_in_own_session` calls) / report phases.
- `.claude/skills/hackernews-source/SKILL.md` — operational policy
  (14 items + divergences) the HN guide became.

**Decisions locked in (don't re-litigate):**

- **Approach A** — LLM brainstorms HN keyword candidates (raw
  keyword + intent + rationale); Python owns ALL mechanics
  (decomposition, 2:1 routing, numericFilters assembly, ≤6 cap).
- **Carry-through across the locked tail, NOT through it.** Capture
  `hn_queries` once in `run_query_expansion`, run the 4 locked tail
  helpers Reddit-only, reattach once. Do NOT thread `hn_queries=`
  through any of the four `model_construct` sites.
- **No retry on HN.** Documented divergence from `source-adapter`
  umbrella; the HN guide is emphatic. Partial success across queries
  is preserved.
- **Per-instance limiter, not a singleton.** Documented divergence
  from `reddit-source`'s shared budget.
- **Both sources every run.** No CLI flags. HN sparsity on non-tech
  industries is graceful (empty `hn_queries` → no-op HN task → done).
- **`JobPlan.hn_queries` permissive default** (no `min_length`).
  HN under-production must not sink the Reddit grounded plan.
- **Parallel fan-out routes around `claim_one`.** CLAUDE.md's
  single-worker assumption stays. `claim_known_task` is the per-id
  race-safe addition; `claim_one` is untouched.

**Smoke verified (real OpenAI gpt-5.4 + real Reddit + real HN
Algolia):** [implementer: fill in the smoke results after running
`discovery run --industry "..." --location ...` end-to-end. Cite
how many HN raw_records landed for at least one tech industry
(e.g. "vector database") and confirm graceful empty for at least
one non-tech industry (e.g. "commercial cleaning"). Confirm wall-
clock < sum of Reddit + HN time, proving the parallel dispatch.]
```

Also append commit rows to the "Commit history (newest first)" table at the top of the section — list the 14 commits this slice produces.

Update the "What's NOT built yet" list to remove "HackerNews" (it's built now) and leave "YouTube, Apollo, Google Places, Yelp, OpenCorporates, trade directories, NewsAPI, Listen Notes, Product Hunt, Census".

Update the "Next slice: open" candidates — Wave 2 (pain + capability classification) climbs to #1 (both Reddit and HN feed Bronze now, but nothing classifies yet).

- [ ] **Step 3: Commit**

```
git add docs/handoff.md
git commit -m "docs(handoff): HackerNews source adapter shipped (2026-05-20)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

**End of Chunk 5 — End of plan.**

After all 5 chunks land, the slice is complete. Final pre-flight before declaring done:

- [ ] `uv run pytest` — all green; new test count visible.
- [ ] `uv run ruff check src tests` — no warnings.
- [ ] `uv run ruff format --check src tests` — no diff.
- [ ] `uv run mypy src/` — `Success: no issues found`.
- [ ] **Real-LLM smoke (tech industry).** `OPENAI_API_KEY` set; `uv run discovery run --industry "vector database" --location US --time-window year`. Expect: `wave 0: planned`, two queued tasks, both reach `done`, Bronze has both `source=reddit` and `source=hackernews` rows. Wall-clock under `max(reddit_time, hn_time) + ~5s`, not the sum.
- [ ] **Real-LLM smoke (non-tech industry).** `uv run discovery run --industry "commercial cleaning" --location US`. Expect: Reddit task with real records, HN task that may be empty or near-empty (graceful sparsity per item 12 of the skill / spec §17 risk 5). No `failed` tasks.
- [ ] **No-LLM smoke (template fallback).** Unset `OPENAI_API_KEY` and re-run. Expect: `wave 0: fallback`, both orchestrators fall back to their deterministic templates, both tasks reach `done`.
- [ ] Update the smoke summary in `docs/handoff.md`'s new section with the actual numbers from these runs.

Then push to the branch and request review. The brainstorming → spec → plan cycle is complete; this plan handed off to @superpowers:subagent-driven-development is the execution path.
