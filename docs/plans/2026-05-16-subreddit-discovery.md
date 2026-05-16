# Subreddit Discovery Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Wave 0's "LLM names subreddits from memory" with "LLM generates subreddit-search phrases → Reddit returns real subreddits → deterministic code filters/ranks → a second grounded LLM call selects subs and designs content queries", folded inside the unchanged `run_query_expansion` signature and its single combined cache entry.

**Architecture:** Wave 0 becomes a multi-step process inside `run_query_expansion`: LLM Call #1 (`subreddit_phrases` prompt, new) emits semantic phrases → a new non-`BaseSource` Reddit `/subreddits/search.json` client returns `SubredditCandidate` DTOs → pure deterministic functions dedupe/filter/rank → LLM Call #2 (`query_expansion` prompt bumped v3→v4 with a grounding section) selects from the supplied table and designs the content queries → the existing deterministic tail (`_drop_invalid_queries` → `MIN_VALID_QUERIES` → `_force_time_window` → `_merge_baseline_subreddits`) runs unchanged. Any failure raises `QueryExpansionError`, reusing the proven template-fallback path. A process-wide shared Reddit rate limiter (prerequisite refactor) ensures sub-search and content-fetch share one 10/min budget.

**Tech Stack:** Python 3.12 (uv), httpx (async), aiolimiter, tenacity-style hand-rolled retry (mirrors `reddit.py`), Pydantic v2, instructor-wrapped OpenAI gpt-5.4, diskcache, pytest + pytest-asyncio (`asyncio_mode=auto`), httpx.MockTransport, loguru, ruff (strict select), mypy strict on `src/`.

---

## Source of truth & policy

- **Spec:** `docs/specs/2026-05-15-subreddit-discovery-design.md` — the approved design (already absorbed one independent review). Do not re-litigate it.
- **Policy skills (hard contracts):** `.claude/skills/reddit-source/SKILL.md` (items 2,3,4,10,17,20,21 apply to the new client), `.claude/skills/llm-station/SKILL.md` (both LLM calls obey the station contract), `.claude/skills/source-adapter/SKILL.md` (the new client is source-style: async/httpx, rate-limited, retried, Pydantic-validated — but returns planning DTOs, never Bronze `RawRecord`s).
- **Locked decisions (do not reopen):** folded inside Wave 0; two LLM calls; deterministic middle (no LLM in ranking); selection adaptive with hard ceiling 30; LLM may pick only from the supplied table; existing `JobPlan` schema, `MIN_VALID_QUERIES=10`, and the deterministic tail are unchanged. Spec §10 explains why thin tables still yield 10–15 queries — do **not** "fix" the floor.

## Deliberate deviations from the spec's file-placement *suggestions*

The spec §4 table gives file-placement *suggestions* ("proposed"); writing-plans produces the concrete *how*. Two deliberate, spec-consistent placement choices, called out so review does not flag them as scope creep:

1. **The deterministic pure pipeline lives in a new `src/discovery/llm/stations/subreddit_selection.py`, not inlined into `query_expansion.py`.** Spec §4 explicitly anticipates this split ("split helpers into `subreddit_selection.py` if it grows") and the project's hard rule is *propose a split at 500 lines*. Adding the whole pipeline inline would push `query_expansion.py` past that. Splitting up front keeps pure functions isolated and independently unit-testable (spec §12's stated priority).
2. **`render_candidate_table` lives in `reddit_subreddits.py` beside the `SubredditCandidate` DTO it projects, not in `subreddit_selection.py`.** Reason: `build_user_message` (a prompt module) must render the table. If the renderer lived in a *station* module, the prompt module would import a station module (an inverted dependency). Placing the renderer with the DTO it projects keeps the dependency direction clean (prompt → sources DTO; station → sources DTO) and still satisfies spec §12 (table projection independently unit-tested — in `test_reddit_subreddits.py`). Spec §5 explicitly says `SubredditCandidate` "lives with the client that produces it"; the projection of that DTO belongs there too.

No behavior, schema, cache contract, or pipeline ordering deviates from the spec.

## File structure (created / modified)

| File | New/Mod | Responsibility |
|---|---|---|
| `src/discovery/sources/reddit_ratelimit.py` | **New** | Process-wide shared Reddit `AsyncLimiter` singleton + `reset_reddit_limiter()` (test-only). |
| `src/discovery/sources/reddit.py` | Mod | `RedditSource` defaults its limiter to the shared singleton instead of self-constructing one. |
| `src/discovery/sources/reddit_subreddits.py` | **New** | `SubredditCandidate` + `PhraseResult` DTOs, `clean_description`, `render_candidate_table`, the `_SubredditT5` response model, and the async `search_subreddits` client (shared limiter, 401/403-raises, retry mirror, partial success, per-request logging). |
| `src/discovery/llm/stations/subreddit_selection.py` | **New** | Pure deterministic pipeline: `dedupe_and_count`, `drop_non_public`, `drop_nsfw`, `subscriber_median`, `drop_below_median`, `with_activity_ratio`, `trim_overflow`, `reject_off_table` + constants. |
| `src/discovery/llm/schemas.py` | Mod | Add `SubredditSearchPhrases` station output. `JobPlan`/`RedditQuerySpec` unchanged in shape. |
| `src/discovery/llm/prompts/subreddit_phrases.py` | **New** | LLM Call #1 prompt: `VERSION="v1"`, `SYSTEM_PROMPT`, `FEW_SHOT_EXAMPLES`, `build_user_message(spec)`. |
| `src/discovery/llm/prompts/query_expansion.py` | Mod | Bump `VERSION` v3→**v4**; add grounding section; change `build_user_message(spec)` → `build_user_message(spec, table)`. |
| `src/discovery/llm/stations/query_expansion.py` | Mod | Orchestrate Call #1 → client → deterministic middle → Call #2 → off-table reject + overflow trim → unchanged tail; combined cache-key string. |
| `tests/unit/sources/conftest.py` | **New** | Autouse fixture: reset the shared limiter before each `sources` test (prevents cross-test budget exhaustion). |
| `tests/unit/sources/test_reddit_ratelimit.py` | **New** | Singleton identity, reset, `RedditSource` default wiring, injection override. |
| `tests/unit/sources/test_reddit_subreddits.py` | **New** | DTO defaults, `clean_description`, `render_candidate_table`, and the client (MockTransport): happy/429/403/partial/empty/logging/limiter-routing. |
| `tests/unit/llm/stations/test_subreddit_selection.py` | **New** | Each pure pipeline function, boundary cases per spec §12. |
| `tests/unit/llm/test_schemas.py` | Mod | Add `SubredditSearchPhrases` validation tests. |
| `tests/unit/llm/test_prompts_subreddit_phrases.py` | **New** | Shape tests for prompt #1. |
| `tests/unit/llm/test_prompts_query_expansion.py` | Mod | New signature (`build_user_message(spec, table)`), grounding substrings, `VERSION == "v4"`. |
| `tests/unit/llm/stations/test_query_expansion.py` | Mod | Rewrite to fake both LLM calls + the sub-search client; full §10 fallback table; combined cache key. |
| `docs/handoff.md` | Mod | Update state, commit table, decisions, next-slice (Task 8). |

**Unchanged on purpose (verified):** `src/discovery/llm/cache.py` (spec §8: zero change to the cache module), `src/discovery/orchestrator/jobs.py` & `tests/unit/test_orchestrator_jobs.py` (they stub `run_query_expansion(spec)->JobPlan`; signature unchanged → stay green), `src/discovery/orchestrator/reddit.py`, `reddit_query_validator.py`, `tests/unit/llm/test_cache.py` (uses literal version strings).

## Pre-flight (run once before Task 1, do not commit)

- [ ] **Health-check the baseline is green**

```
uv run pytest
uv run ruff check .
uv run mypy src/
```
Expected: pytest `~165 passed` (handoff says 148 at 2026-05-15; later commits added ~17 — any all-green count is fine), ruff `All checks passed!`, mypy `Success: no issues found`. If any fail, stop and report — do not start on a red baseline.

---

## Chunk 1: Shared Reddit limiter (spec step 0)

### Task 1: Process-wide Reddit rate-limiter singleton

Prerequisite refactor. Today `RedditSource.__init__` default-constructs its own `AsyncLimiter(10, 60.1)` and the station constructs none. Sub-search (Wave 0) and content-fetch (Wave 1) run in the same process and must share ONE 10/min budget (spec §11 item 3). This is a real change to shipped code — own commit, reviewed deliberately.

**Files:**
- Create: `src/discovery/sources/reddit_ratelimit.py`
- Modify: `src/discovery/sources/reddit.py` (remove `_DEFAULT_RATE`; default limiter to the singleton; docstring)
- Create: `tests/unit/sources/conftest.py`
- Test: `tests/unit/sources/test_reddit_ratelimit.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/sources/test_reddit_ratelimit.py`:

```python
"""Tests for the process-wide shared Reddit limiter (spec §11 item 3)."""

from __future__ import annotations

import httpx

from discovery.sources.reddit import RedditSource
from discovery.sources.reddit_ratelimit import (
    get_reddit_limiter,
    reset_reddit_limiter,
)


class TestSingleton:
    def test_get_returns_same_instance(self) -> None:
        assert get_reddit_limiter() is get_reddit_limiter()

    def test_reset_makes_a_fresh_instance(self) -> None:
        first = get_reddit_limiter()
        reset_reddit_limiter()
        assert get_reddit_limiter() is not first


class TestRedditSourceWiring:
    async def test_defaults_to_the_shared_singleton(self) -> None:
        source = RedditSource(user_agent="discovery-tests/0.1")
        try:
            assert source._limiter is get_reddit_limiter()
        finally:
            await source.aclose()

    async def test_injected_limiter_overrides_the_singleton(self) -> None:
        from aiolimiter import AsyncLimiter

        injected = AsyncLimiter(99, 1)
        source = RedditSource(
            user_agent="discovery-tests/0.1",
            client=httpx.AsyncClient(),
            limiter=injected,
        )
        try:
            assert source._limiter is injected
            assert source._limiter is not get_reddit_limiter()
        finally:
            await source.aclose()
```

Create `tests/unit/sources/conftest.py`:

```python
"""Autouse: each `sources` test starts with a fresh shared Reddit
limiter. The limiter is now a process-wide singleton
(discovery.sources.reddit_ratelimit). Today's reddit tests stay under
the 10/60.1s budget even sharing it, but once the sub-search client
tests land (Task 4) the per-test request count against the SAME
singleton exceeds the budget — without a per-test reset a later test
would block on a real ~60s sleep.
"""

from __future__ import annotations

import pytest

from discovery.sources.reddit_ratelimit import reset_reddit_limiter


@pytest.fixture(autouse=True)
def _fresh_reddit_limiter() -> None:
    reset_reddit_limiter()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sources/test_reddit_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery.sources.reddit_ratelimit'`.

- [ ] **Step 3: Create the limiter module**

Create `src/discovery/sources/reddit_ratelimit.py`:

```python
"""Process-wide Reddit rate limiter (skill item 3).

Sub-discovery (Wave 0) and content-fetch (Wave 1) run in the SAME
process and share ONE 10-requests/minute unauthenticated Reddit budget.
If each component default-constructed its own `AsyncLimiter`, the real
request rate would silently double and earn 429s. This module owns the
single shared limiter; both `RedditSource` and the subreddit-search
client default to it.

Memoized into a module dict (not a `global`) — the same pattern as the
lazy OpenAI client singleton in `discovery.llm.client`.
`reset_reddit_limiter()` exists only so tests start each case with a
fresh budget; production never calls it.
"""

from __future__ import annotations

from aiolimiter import AsyncLimiter

# Skill item 3: ~10 requests/min unauthenticated. 60.1s (not 60.0) so
# clock skew can't bunch two requests into the same wall-clock second.
REDDIT_RATE: tuple[int, float] = (10, 60.1)

_singleton: dict[str, AsyncLimiter] = {}


def get_reddit_limiter() -> AsyncLimiter:
    """Return the process-wide shared Reddit `AsyncLimiter` (memoized)."""
    if "limiter" not in _singleton:
        _singleton["limiter"] = AsyncLimiter(REDDIT_RATE[0], REDDIT_RATE[1])
    return _singleton["limiter"]


def reset_reddit_limiter() -> None:
    """Drop the memoized limiter so the next `get_reddit_limiter()`
    builds a fresh one. Test-only.
    """
    _singleton.clear()
```

- [ ] **Step 4: Wire `RedditSource` to the singleton**

In `src/discovery/sources/reddit.py`:

Remove the now-unused constant (lines ~37–39):

```python
# Skill item 3: ~10 requests/min unauthenticated. Use 60.1s to avoid
# bunching at second boundaries.
_DEFAULT_RATE = (10, 60.1)
```

Add to the imports block (next to `from discovery.sources.base import BaseSource, RawRecord`):

```python
from discovery.sources.reddit_ratelimit import get_reddit_limiter
```

Change the limiter default line in `__init__` from:

```python
        self._limiter = limiter or AsyncLimiter(_DEFAULT_RATE[0], _DEFAULT_RATE[1])
```
to:
```python
        self._limiter = limiter if limiter is not None else get_reddit_limiter()
```

Update the `limiter :` docstring entry in the class docstring from `Optional `AsyncLimiter`. Default = 10 req / 60.1s.` to:

```python
    limiter :
        Optional `AsyncLimiter`. Default = the process-wide shared
        Reddit limiter (skill item 3 — sub-search and content-fetch
        must not each construct their own).
```

(Keep `from aiolimiter import AsyncLimiter` — still used for the param type hint. Keep the unrelated `rate_limit = (10, 60)` class attribute; it is `BaseSource` declared metadata, not the live limiter.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sources/test_reddit_ratelimit.py tests/unit/sources/test_reddit.py -v`
Expected: PASS — all `test_reddit_ratelimit` cases pass and the full existing `test_reddit.py` suite still passes (the autouse `conftest.py` reset keeps the now-shared limiter from blocking across cases).

- [ ] **Step 6: Run all checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green. (If `ruff format --check` flags files, run `uv run ruff format .` and re-stage.)

- [ ] **Step 7: Commit**

```
git add src/discovery/sources/reddit_ratelimit.py src/discovery/sources/reddit.py tests/unit/sources/conftest.py tests/unit/sources/test_reddit_ratelimit.py
git commit -m "refactor(sources): shared process-wide Reddit limiter (spec step 0)

Sub-search (Wave 0) and content-fetch (Wave 1) now share ONE 10/min
budget via a memoized singleton instead of each constructing its own
AsyncLimiter (skill item 3). RedditSource defaults to it; test
injection unchanged. Autouse fixture resets it per sources test.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Chunk 2: Deterministic core — DTOs + pure pipeline (spec step 1)

### Task 2: `SubredditCandidate` / `PhraseResult` DTOs, `clean_description`, `render_candidate_table`

DTOs live with the client that produces them (spec §4/§5). `render_candidate_table` lives here too (see "Deliberate deviations" above). The async client itself is Task 4 — this task is DTOs + pure helpers only.

**Files:**
- Create: `src/discovery/sources/reddit_subreddits.py` (DTOs + helpers only)
- Test: `tests/unit/sources/test_reddit_subreddits.py` (DTO/helper section)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/sources/test_reddit_subreddits.py`:

```python
"""Tests for `discovery.sources.reddit_subreddits`.

Two layers, mirroring test_reddit.py:
1. Pure helpers/DTOs — no HTTP, no async.
2. `search_subreddits` — httpx.MockTransport, injected no-op sleep.
"""

from __future__ import annotations

from discovery.sources.reddit_subreddits import (
    PhraseResult,
    SubredditCandidate,
    clean_description,
    render_candidate_table,
)


class TestCleanDescription:
    def test_collapses_whitespace(self) -> None:
        assert clean_description("a   b\n\tc") == "a b c"

    def test_truncates_long_with_ellipsis(self) -> None:
        out = clean_description("x" * 500)
        assert len(out) == 301  # 300 chars + the single ellipsis char
        assert out.endswith("…")

    def test_short_passes_through(self) -> None:
        assert clean_description("  short  ") == "short"


class TestSubredditCandidate:
    def test_defaults(self) -> None:
        c = SubredditCandidate(name="startups")
        assert c.subscribers == 0
        assert c.active_user_count == 0
        assert c.activity_ratio == 0.0
        assert c.public_description == ""
        assert c.matched_phrases == 0
        assert c.subreddit_type == "public"
        assert c.over18 is False

    def test_is_frozen(self) -> None:
        import pytest
        from pydantic import ValidationError

        c = SubredditCandidate(name="x")
        with pytest.raises(ValidationError):
            c.name = "y"  # type: ignore[misc]


class TestPhraseResult:
    def test_holds_phrase_and_candidates(self) -> None:
        pr = PhraseResult(
            phrase="cleaning business",
            candidates=[SubredditCandidate(name="CleaningTips")],
        )
        assert pr.phrase == "cleaning business"
        assert pr.candidates[0].name == "CleaningTips"

    def test_candidates_default_empty(self) -> None:
        assert PhraseResult(phrase="p").candidates == []


class TestRenderCandidateTable:
    def _c(self, **kw: object) -> SubredditCandidate:
        base: dict[str, object] = {
            "name": "startups",
            "subscribers": 1000,
            "active_user_count": 50,
            "activity_ratio": 0.05,
            "public_description": "founders talk shop",
            "matched_phrases": 3,
        }
        base.update(kw)
        return SubredditCandidate(**base)  # type: ignore[arg-type]

    def test_header_has_exactly_six_columns_in_order(self) -> None:
        out = render_candidate_table([self._c()])
        header = out.splitlines()[0]
        assert header.split("\t") == [
            "name",
            "subscribers",
            "active_user_count",
            "activity_ratio",
            "public_description",
            "matched_phrases",
        ]

    def test_one_row_per_candidate_six_fields(self) -> None:
        out = render_candidate_table([self._c(), self._c(name="saas")])
        rows = out.splitlines()[1:]
        assert len(rows) == 2
        assert all(len(r.split("\t")) == 6 for r in rows)
        assert rows[1].split("\t")[0] == "saas"

    def test_neutralizes_tabs_and_newlines_in_description(self) -> None:
        out = render_candidate_table(
            [self._c(public_description="a\tb\nc")]
        )
        row = out.splitlines()[1]
        # description column must not introduce extra tab/newline columns
        assert len(row.split("\t")) == 6
        assert "\n" not in row

    def test_empty_list_renders_header_only(self) -> None:
        out = render_candidate_table([])
        assert out.splitlines() == [
            "name\tsubscribers\tactive_user_count\tactivity_ratio\tpublic_description\tmatched_phrases"
        ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sources/test_reddit_subreddits.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery.sources.reddit_subreddits'`.

- [ ] **Step 3: Create the module (DTOs + helpers only)**

Create `src/discovery/sources/reddit_subreddits.py`:

```python
"""Reddit subreddit-discovery client and its DTOs.

NOT a `BaseSource`. This hits Reddit's `/subreddits/search.json`
endpoint to find *real, currently-existing* subreddits for Wave 0
query planning. The result is a planning artifact, never Bronze
`raw_records` data — so it returns `SubredditCandidate` DTOs, not
`RawRecord`s. It still obeys the source-adapter contract (async httpx,
shared rate limiter, retry, Pydantic-validated response) and the
reddit-source skill (User-Agent, 6.1s pacing, 401/403 raise, partial
success, per-request logging).

See `.claude/skills/reddit-source/SKILL.md` (items 2,3,4,10,17,20,21)
and `docs/specs/2026-05-15-subreddit-discovery-design.md`.

This task adds the DTOs + pure helpers; `search_subreddits` (the async
client) is added in the next task.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_DESCRIPTION_LIMIT = 300

# The 6 columns the LLM (Call #2) sees, in order. `subreddit_type` and
# `over18` are carried on the DTO for deterministic filtering only and
# are intentionally NOT in this projection (spec §5).
_TABLE_COLUMNS: tuple[str, ...] = (
    "name",
    "subscribers",
    "active_user_count",
    "activity_ratio",
    "public_description",
    "matched_phrases",
)


def clean_description(raw: str) -> str:
    """Collapse whitespace runs and truncate to ~300 chars.

    `public_description` is the LLM's primary relevance signal (spec
    §6); a few hundred chars is plenty and keeps the rendered table
    compact (spec §5: 25 raw t5 objects ≈ 80k tokens, the projection a
    few hundred).
    """
    collapsed = re.sub(r"\s+", " ", raw).strip()
    if len(collapsed) <= _DESCRIPTION_LIMIT:
        return collapsed
    return collapsed[:_DESCRIPTION_LIMIT] + "…"


class SubredditCandidate(BaseModel):
    """One deduped, surviving subreddit considered for Wave 0 selection.

    Six fields are projected into the table the LLM sees;
    `subreddit_type`/`over18` are filter-only and dropped before the LLM
    (spec §5). `matched_phrases` and `activity_ratio` are populated by
    the deterministic pipeline, not at client parse time.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    subscribers: int = 0
    active_user_count: int = 0
    activity_ratio: float = 0.0
    public_description: str = ""
    matched_phrases: int = 0
    subreddit_type: str = "public"
    over18: bool = False


class PhraseResult(BaseModel):
    """Raw per-phrase search result. One entry per phrase request that
    succeeded (failed phrases omitted — partial success, skill item 17).
    Candidates carry raw fields only; the pipeline sets `matched_phrases`
    and `activity_ratio` later.
    """

    model_config = ConfigDict(frozen=True)

    phrase: str
    candidates: list[SubredditCandidate] = Field(default_factory=list)


def render_candidate_table(candidates: list[SubredditCandidate]) -> str:
    """Render candidates as a compact tab-delimited table — header line
    plus one row per subreddit, exactly the 6 columns in
    `_TABLE_COLUMNS` (spec §5). NOT raw JSON: compaction is mandatory,
    not an optimization. Tabs/newlines inside the description are
    replaced with spaces so the column count stays exactly 6.
    """
    lines = ["\t".join(_TABLE_COLUMNS)]
    for c in candidates:
        desc = c.public_description.replace("\t", " ").replace("\n", " ")
        lines.append(
            "\t".join(
                [
                    c.name,
                    str(c.subscribers),
                    str(c.active_user_count),
                    str(c.activity_ratio),
                    desc,
                    str(c.matched_phrases),
                ]
            )
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sources/test_reddit_subreddits.py -v`
Expected: PASS — all DTO/helper/render tests pass.

- [ ] **Step 5: Run all checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green.

- [ ] **Step 6: Commit**

```
git add src/discovery/sources/reddit_subreddits.py tests/unit/sources/test_reddit_subreddits.py
git commit -m "feat(sources): SubredditCandidate/PhraseResult DTOs + table render

Planning DTOs for subreddit discovery (spec §5). render_candidate_table
projects exactly the 6 LLM-facing columns; clean_description does the
whitespace-collapse + ~300char truncation. Client added next task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 3: Deterministic pipeline pure functions

The "deterministic middle" (spec §3, §7 steps 2–7, 9 and the §10 off-table defensive filter). Pure functions, no LLM, no I/O — the highest-value, most-testable layer (spec §12).

**Files:**
- Create: `src/discovery/llm/stations/subreddit_selection.py`
- Test: `tests/unit/llm/stations/test_subreddit_selection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/llm/stations/test_subreddit_selection.py`:

```python
"""Tests for the deterministic subreddit pipeline (spec §7).

Each function is pure; boundary cases per spec §12.
"""

from __future__ import annotations

from discovery.llm.stations.subreddit_selection import (
    DRASTIC_FLOOR_DIVISOR,
    SELECTION_CEILING,
    dedupe_and_count,
    drop_below_median,
    drop_non_public,
    drop_nsfw,
    reject_off_table,
    subscriber_median,
    trim_overflow,
    with_activity_ratio,
)
from discovery.sources.reddit_subreddits import PhraseResult, SubredditCandidate


def _c(name: str, **kw: object) -> SubredditCandidate:
    base: dict[str, object] = {"name": name}
    base.update(kw)
    return SubredditCandidate(**base)  # type: ignore[arg-type]


class TestDedupeAndCount:
    def test_collapses_to_unique_name_case_insensitive(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("Startups"), _c("saas")]),
            PhraseResult(phrase="p2", candidates=[_c("startups")]),
        ]
        out = dedupe_and_count(results)
        names = sorted(c.name for c in out)
        assert names == ["Startups", "saas"]  # first-seen casing kept

    def test_matched_phrases_counts_distinct_phrases(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("startups")]),
            PhraseResult(phrase="p2", candidates=[_c("startups")]),
            PhraseResult(phrase="p2", candidates=[_c("startups")]),  # dup phrase
        ]
        out = dedupe_and_count(results)
        assert len(out) == 1
        assert out[0].matched_phrases == 2  # p1, p2 — not 3

    def test_first_occurrence_wins_for_other_fields(self) -> None:
        results = [
            PhraseResult(phrase="p1", candidates=[_c("x", subscribers=100)]),
            PhraseResult(phrase="p2", candidates=[_c("x", subscribers=999)]),
        ]
        out = dedupe_and_count(results)
        assert out[0].subscribers == 100


class TestDropNonPublic:
    def test_keeps_public_and_restricted_only(self) -> None:
        cands = [
            _c("a", subreddit_type="public"),
            _c("b", subreddit_type="restricted"),
            _c("c", subreddit_type="private"),
            _c("d", subreddit_type="archived"),
            _c("e", subreddit_type="quarantined"),
        ]
        kept = {c.name for c in drop_non_public(cands)}
        assert kept == {"a", "b"}


class TestDropNsfw:
    def test_drops_over18(self) -> None:
        cands = [_c("a", over18=False), _c("b", over18=True)]
        assert [c.name for c in drop_nsfw(cands)] == ["a"]


class TestSubscriberMedian:
    def test_odd_count(self) -> None:
        assert subscriber_median([_c("a", subscribers=1), _c("b", subscribers=3), _c("c", subscribers=2)]) == 2.0

    def test_even_count(self) -> None:
        assert subscriber_median([_c("a", subscribers=1), _c("b", subscribers=3)]) == 2.0

    def test_empty_is_zero(self) -> None:
        assert subscriber_median([]) == 0.0


class TestDropBelowMedian:
    def test_drops_strictly_below_floor_keeps_equal(self) -> None:
        # median 1000 → floor = 1000 / 10 = 100. 100 kept, 99 dropped.
        cands = [_c("keep", subscribers=100), _c("drop", subscribers=99)]
        kept = {c.name for c in drop_below_median(cands, 1000.0)}
        assert kept == {"keep"}

    def test_zero_median_keeps_all(self) -> None:
        # value-equality, not identity: drop_below_median returns a
        # fresh list on the median<=0 path. Asserting == (pydantic
        # compares candidates by value) guards against a future "fix"
        # that returns the input list directly (a subtle aliasing bug).
        cands = [_c("a", subscribers=0)]
        assert drop_below_median(cands, 0.0) == cands

    def test_divisor_constant_is_ten(self) -> None:
        assert DRASTIC_FLOOR_DIVISOR == 10


class TestWithActivityRatio:
    def test_normal_ratio_rounded_4dp(self) -> None:
        out = with_activity_ratio([_c("a", subscribers=3000, active_user_count=10)])
        assert out[0].activity_ratio == round(10 / 3000, 4)

    def test_missing_active_is_zero(self) -> None:
        out = with_activity_ratio([_c("a", subscribers=1000, active_user_count=0)])
        assert out[0].activity_ratio == 0.0

    def test_zero_subscribers_guarded_to_zero(self) -> None:
        out = with_activity_ratio([_c("a", subscribers=0, active_user_count=5)])
        assert out[0].activity_ratio == 0.0


class TestTrimOverflow:
    def test_passthrough_at_or_below_ceiling(self) -> None:
        names = [f"s{i}" for i in range(SELECTION_CEILING)]
        assert trim_overflow(names) == names

    def test_keeps_first_30_in_order_when_over(self) -> None:
        names = [f"s{i}" for i in range(45)]
        out = trim_overflow(names)
        assert out == [f"s{i}" for i in range(30)]

    def test_ceiling_constant_is_30(self) -> None:
        assert SELECTION_CEILING == 30


class TestRejectOffTable:
    def test_drops_names_not_in_table_case_insensitive(self) -> None:
        table = [_c("Startups"), _c("saas")]
        assert reject_off_table(["startups", "ghost", "SAAS"], table) == [
            "startups",
            "SAAS",
        ]

    def test_preserves_selection_order(self) -> None:
        table = [_c("a"), _c("b"), _c("c")]
        assert reject_off_table(["c", "a", "b"], table) == ["c", "a", "b"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/llm/stations/test_subreddit_selection.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery.llm.stations.subreddit_selection'`.

- [ ] **Step 3: Create the pipeline module**

Create `src/discovery/llm/stations/subreddit_selection.py`:

```python
"""Deterministic subreddit pipeline — the "no LLM" middle of Wave 0.

Pure functions only (no I/O, no LLM). Order is fixed by spec §7:

    dedupe + consensus → drop non-public → drop NSFW → median →
    drop drastically-below-median → activity_ratio
    → (LLM Call #2 selects) → reject off-table → overflow trim

`render_candidate_table` (the projection step) lives in
`discovery.sources.reddit_subreddits` beside the DTO it projects so the
prompt module can import it without inverting the layer direction.
"""

from __future__ import annotations

from discovery.sources.reddit_subreddits import PhraseResult, SubredditCandidate

# Spec §7 step 6: gentle relative floor. subscribers < median/10 → drop.
# Kills dead/junk without decapitating small niche communities the LLM
# should still judge. Tunable later if Item-21 data warrants (spec §13).
DRASTIC_FLOOR_DIVISOR: int = 10

# Spec §2.4 / §7 step 9: adaptive selection, hard ceiling 30.
SELECTION_CEILING: int = 30

_PUBLIC_TYPES: frozenset[str] = frozenset({"public", "restricted"})


def dedupe_and_count(results: list[PhraseResult]) -> list[SubredditCandidate]:
    """Collapse to unique subreddit (case-insensitive name); set
    `matched_phrases` = number of DISTINCT phrases whose result set
    contained it (spec §7 step 2). First occurrence wins for every other
    field. Dedup MUST precede the median — duplicates would skew it.
    """
    first_seen: dict[str, SubredditCandidate] = {}
    phrases_for: dict[str, set[str]] = {}
    for res in results:
        for cand in res.candidates:
            key = cand.name.lower()
            phrases_for.setdefault(key, set()).add(res.phrase)
            if key not in first_seen:
                first_seen[key] = cand
    return [
        cand.model_copy(update={"matched_phrases": len(phrases_for[key])})
        for key, cand in first_seen.items()
    ]


def drop_non_public(cands: list[SubredditCandidate]) -> list[SubredditCandidate]:
    """Spec §7 step 3: keep `subreddit_type ∈ {public, restricted}`.
    `restricted` is READable (only posting is gated) — keep it.
    """
    return [c for c in cands if c.subreddit_type in _PUBLIC_TYPES]


def drop_nsfw(cands: list[SubredditCandidate]) -> list[SubredditCandidate]:
    """Spec §7 step 4: drop `over18` (defense in depth — the request
    also sets `include_over_18=false`; neither alone is fully reliable).
    """
    return [c for c in cands if not c.over18]


def subscriber_median(cands: list[SubredditCandidate]) -> float:
    """Median of `subscribers` over `cands` (spec §7 step 5). Empty →
    0.0; the caller checks emptiness separately and raises.
    """
    if not cands:
        return 0.0
    xs = sorted(c.subscribers for c in cands)
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return float(xs[mid])
    return (xs[mid - 1] + xs[mid]) / 2


def drop_below_median(
    cands: list[SubredditCandidate], median: float
) -> list[SubredditCandidate]:
    """Spec §7 step 6: drop `subscribers < median / DRASTIC_FLOOR_DIVISOR`
    (strictly below; equal is kept). `median <= 0` → no-op (nothing to
    compare against).
    """
    if median <= 0:
        return list(cands)
    floor = median / DRASTIC_FLOOR_DIVISOR
    return [c for c in cands if c.subscribers >= floor]


def with_activity_ratio(
    cands: list[SubredditCandidate],
) -> list[SubredditCandidate]:
    """Spec §7 step 7: `activity_ratio = active_user_count / subscribers`,
    rounded ~4dp. Guard divide-by-zero AND missing active → 0.0.
    """
    out: list[SubredditCandidate] = []
    for c in cands:
        ratio = (
            round(c.active_user_count / c.subscribers, 4)
            if c.subscribers > 0
            else 0.0
        )
        out.append(c.model_copy(update={"activity_ratio": ratio}))
    return out


def reject_off_table(
    selected: list[str], table: list[SubredditCandidate]
) -> list[str]:
    """Spec §10 defensive filter: drop any selected sub NOT present in
    the supplied table (case-insensitive — Reddit names are
    case-insensitive). The grounding prompt forbids off-table picks;
    this enforces it deterministically if the LLM slips. Selection
    order is preserved.
    """
    allowed = {c.name.lower() for c in table}
    return [s for s in selected if s.lower() in allowed]


def trim_overflow(selected: list[str]) -> list[str]:
    """Spec §7 step 9: keep the LLM's first `SELECTION_CEILING` in its
    emitted order. `JobPlan.reddit_subreddits` is an ordered list, so
    the spec's "tie-break only if order is ambiguous" branch
    (matched_phrases desc, activity_ratio desc) is unreachable here and
    is intentionally not implemented (YAGNI).
    """
    return selected[:SELECTION_CEILING]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/llm/stations/test_subreddit_selection.py -v`
Expected: PASS — every pipeline function test passes including all boundary cases.

- [ ] **Step 5: Run all checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green.

- [ ] **Step 6: Commit**

```
git add src/discovery/llm/stations/subreddit_selection.py tests/unit/llm/stations/test_subreddit_selection.py
git commit -m "feat(llm): deterministic subreddit pipeline (spec §7)

Pure functions: dedupe+consensus, non-public/NSFW filters, subscriber
median, drastic-below-median drop (divisor 10), activity_ratio,
off-table reject, overflow trim (ceiling 30). No LLM in the ranking.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Chunk 3: Reddit `/subreddits/search` client (spec step 2)

### Task 4: `search_subreddits` async client

Adds the async client to `reddit_subreddits.py`. Shared limiter from Task 1; 401/403 **raise** (never empty — spec §11 item 4); retry policy mirrors `reddit.py._fetch_with_retries`; partial success across phrases (skill item 17); empty children = `ok_empty` not failure (skill item 20); per-request structured log (skill item 21).

> **Spec-sanctioned duplication:** the retry loop mirrors (does not import) `RedditSource._fetch_with_retries`. Spec §11 item 4 says "Mirror the existing `_fetch_with_retries`". Extracting a shared retry helper would touch shipped `reddit.py` beyond step 0's deliberately narrow scope, so it is out of scope here. The one behavioral difference is mandatory: **401/403 raise before results are interpreted** (spec §11 item 4) — a 403 silently mapped to "0 results" would be indistinguishable from a legitimate empty search. A DRY follow-up (shared `reddit_http` retry helper) is noted for `docs/handoff.md` in Task 8.

**Files:**
- Modify: `src/discovery/sources/reddit_subreddits.py` (append client + response model + retry helpers)
- Modify: `tests/unit/sources/test_reddit_subreddits.py` (append client section)

- [ ] **Step 1: Write the failing tests (append to the existing file)**

Append to `tests/unit/sources/test_reddit_subreddits.py`:

```python
# --- Client integration --------------------------------------------------

from collections.abc import Callable  # noqa: E402

import httpx  # noqa: E402
from loguru import logger as _loguru  # noqa: E402

from discovery.sources.reddit_subreddits import search_subreddits  # noqa: E402


def _t5(name: str, **over: object) -> dict[str, object]:
    data: dict[str, object] = {
        "display_name": name,
        "subscribers": 1000,
        "active_user_count": 50,
        "subreddit_type": "public",
        "over18": False,
        "public_description": f"{name} community",
    }
    data.update(over)
    return {"kind": "t5", "data": data}


def _listing(*names: str) -> dict[str, object]:
    return {"kind": "Listing", "data": {"children": [_t5(n) for n in names], "after": None}}


async def _noop_sleep(_: float) -> None:
    return None


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestSearchSubredditsHappyPath:
    async def test_returns_one_phraseresult_per_phrase(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_listing("startups", "saas"))

        out = await search_subreddits(
            ["a", "b"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert [r.phrase for r in out] == ["a", "b"]
        assert {c.name for c in out[0].candidates} == {"startups", "saas"}
        assert out[0].candidates[0].public_description != ""

    async def test_request_url_has_required_params(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, json=_listing())

        await search_subreddits(
            ["food truck"],
            user_agent="my-app/1.0 (u/me)",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert "/subreddits/search.json" in seen["url"]
        assert "q=food+truck" in seen["url"] or "q=food%20truck" in seen["url"]
        assert "limit=100" in seen["url"]
        assert "raw_json=1" in seen["url"]
        assert "include_over_18=false" in seen["url"]
        assert seen["ua"] == "my-app/1.0 (u/me)"


class TestSearchSubredditsEmpty:
    async def test_empty_children_is_ok_empty_not_failure(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"kind": "Listing", "data": {"children": []}})

        out = await search_subreddits(
            ["x"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert len(out) == 1
        assert out[0].candidates == []


class TestSearchSubredditsRetry:
    async def test_429_then_200_retries_and_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "1"})
            return httpx.Response(200, json=_listing("startups"))

        out = await search_subreddits(
            ["x"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
        )
        assert calls["n"] == 2
        assert out[0].candidates[0].name == "startups"


class TestSearchSubreddits403Raises:
    async def test_403_raises_not_empty(self) -> None:
        import pytest

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        with pytest.raises(httpx.HTTPStatusError):
            await search_subreddits(
                ["x"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
            )


class TestSearchSubredditsPartialSuccess:
    async def test_one_phrase_failing_does_not_kill_the_others(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "q=bad" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(200, json=_listing("startups"))

        out = await search_subreddits(
            ["good1", "bad", "good2"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
            max_retries=0,
        )
        assert [r.phrase for r in out] == ["good1", "good2"]

    async def test_total_wipeout_raises_first_error(self) -> None:
        import pytest

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(httpx.HTTPError):
            await search_subreddits(
                ["a", "b"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
                max_retries=0,
            )


class TestSearchSubredditsLimiterRouting:
    async def test_every_request_goes_through_the_injected_limiter(self) -> None:
        entered = {"n": 0}

        class _CountingLimiter:
            async def __aenter__(self) -> None:
                entered["n"] += 1

            async def __aexit__(self, *exc: object) -> None:
                return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_listing("startups"))

        await search_subreddits(
            ["a", "b", "c"],
            user_agent="discovery-tests/0.1",
            client=_client(handler),
            sleep=_noop_sleep,
            limiter=_CountingLimiter(),  # type: ignore[arg-type]
        )
        assert entered["n"] == 3  # one limiter acquisition per phrase


class TestSearchSubredditsLogging:
    async def test_per_request_structured_log(self) -> None:
        captured: list[dict[str, object]] = []

        def sink(message: object) -> None:
            captured.append(dict(message.record["extra"]))  # type: ignore[attr-defined]

        sink_id = _loguru.add(sink, level="DEBUG")
        try:

            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_listing("startups", "saas"))

            await search_subreddits(
                ["x"],
                user_agent="discovery-tests/0.1",
                client=_client(handler),
                sleep=_noop_sleep,
            )
            logs = [c for c in captured if "url" in c and "count_after_filter" in c]
            assert logs, f"no per-request log; captured: {captured}"
            log = logs[0]
            assert log["status"] == 200
            assert log["count_before_filter"] == 2
            assert log["count_after_filter"] == 2
            assert log["phrase"] == "x"
            assert "/subreddits/search.json" in log["url"]
        finally:
            _loguru.remove(sink_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/sources/test_reddit_subreddits.py -v`
Expected: FAIL — `ImportError: cannot import name 'search_subreddits'`.

- [ ] **Step 3: Append the client to `reddit_subreddits.py`**

Add these imports to the existing `from __future__` / import block at the top of `src/discovery/sources/reddit_subreddits.py`:

```python
import asyncio
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger
from pydantic import ValidationError

from discovery.sources.reddit_ratelimit import get_reddit_limiter
```

(Keep the existing `import re` and `from pydantic import BaseModel, ConfigDict, Field`.)

Append to the end of `src/discovery/sources/reddit_subreddits.py`:

```python
_SUBREDDITS_SEARCH_URL = "https://www.reddit.com/subreddits/search.json"
_MAX_BACKOFF = 300.0


class _SubredditT5(BaseModel):
    """Minimal validated view of one Reddit `t5` object. Only the fields
    we need (source-adapter contract: validate the shape you got).
    `active_user_count` and `accounts_active` are two spellings of the
    same signal; prefer the former, fall back to the latter, default 0.
    """

    model_config = ConfigDict(extra="ignore")

    display_name: str
    subscribers: int | None = 0
    active_user_count: int | None = None
    accounts_active: int | None = None
    subreddit_type: str | None = "public"
    over18: bool | None = False
    public_description: str | None = ""

    def to_candidate(self) -> SubredditCandidate:
        active = (
            self.active_user_count
            if self.active_user_count is not None
            else (self.accounts_active or 0)
        )
        return SubredditCandidate(
            name=self.display_name,
            subscribers=self.subscribers or 0,
            active_user_count=active or 0,
            public_description=clean_description(self.public_description or ""),
            subreddit_type=self.subreddit_type or "public",
            over18=bool(self.over18),
        )


def _build_url(phrase: str) -> str:
    """`/subreddits/search.json` with the spec §7-step-1 params. `sort`
    is omitted — Reddit's sub-search `sort` is non-functional (spec §1);
    all ranking is ours.
    """
    params = {
        "q": phrase,
        "limit": "100",
        "raw_json": "1",
        "include_over_18": "false",
    }
    return f"{_SUBREDDITS_SEARCH_URL}?{urlencode(params)}"


def _backoff_seconds(attempt: int) -> float:
    """5s, 10s, 20s, capped at 300s. `2.0 ** attempt` keeps the result
    unambiguously float (mirrors reddit.py)."""
    return min(5.0 * (2.0**attempt), _MAX_BACKOFF)


def _retry_after_or_backoff(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            wait = float(retry_after)
        except ValueError:
            wait = _backoff_seconds(attempt)
    else:
        wait = _backoff_seconds(attempt)
    return max(1.0, min(wait, _MAX_BACKOFF))  # clamp 1s..5min (skill item 4)


async def _get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    user_agent: str,
    sleep: Callable[[float], Awaitable[None]],
    max_retries: int,
) -> httpx.Response:
    """Mirror of reddit.py's retry policy (skill item 4), with the
    mandatory difference that 401/403 RAISE before results are
    interpreted (spec §11 item 4): a 403 silently mapped to empty would
    be indistinguishable from a legitimate empty search (skill item 20).

    - 401/403 → raise immediately (auth/IP block; no retry).
    - 429 → retry, honour Retry-After (clamped 1s..5min).
    - 5xx / network → retry with exponential backoff.
    - other 4xx (404/414/…) → raise (unexpected; surface it).
    """
    last_exc: httpx.HTTPError | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(url, headers={"User-Agent": user_agent})
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < max_retries:
                await sleep(_backoff_seconds(attempt))
                continue
            raise

        if response.status_code in (401, 403):
            response.raise_for_status()

        if response.status_code == 429:
            if attempt >= max_retries:
                response.raise_for_status()
            await sleep(_retry_after_or_backoff(response, attempt))
            continue

        if 500 <= response.status_code < 600:
            if attempt >= max_retries:
                response.raise_for_status()
            await sleep(_backoff_seconds(attempt))
            continue

        response.raise_for_status()  # any other 4xx → raise (spec §11.4)
        return response

    assert last_exc is not None  # unreachable; typecheck-friendly
    raise last_exc


def _parse_listing(payload: dict[str, object]) -> list[SubredditCandidate]:
    data = payload.get("data") or {}
    children = data.get("children", []) if isinstance(data, dict) else []
    out: list[SubredditCandidate] = []
    for child in children:
        raw = child.get("data", {}) if isinstance(child, dict) else {}
        try:
            out.append(_SubredditT5.model_validate(raw).to_candidate())
        except ValidationError:
            logger.debug("skipping malformed t5 object", raw=raw)
    return out


async def search_subreddits(
    phrases: list[str],
    *,
    user_agent: str,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    limiter: AsyncLimiter | None = None,
    max_retries: int = 3,
    timeout: float = 30.0,
) -> list[PhraseResult]:
    """One `/subreddits/search.json` request per phrase (spec §7 step 1).

    Partial success (skill item 17): a phrase failing after retries does
    not kill the others. Only a TOTAL wipeout (every phrase failed)
    raises (the first error, so the station maps it to
    `QueryExpansionError`). Empty children = `ok_empty`, not failure
    (skill item 20). Shares the process-wide Reddit limiter (spec §11
    item 3) unless one is injected.
    """
    own_client = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=timeout)
    lim = limiter if limiter is not None else get_reddit_limiter()

    results: list[PhraseResult] = []
    errors: list[Exception] = []
    try:
        for phrase in phrases:
            url = _build_url(phrase)
            started = time.monotonic()
            try:
                async with lim:
                    response = await _get_with_retries(
                        http,
                        url,
                        user_agent=user_agent,
                        sleep=sleep,
                        max_retries=max_retries,
                    )
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("subreddit search failed", phrase=phrase, error=str(exc))
                errors.append(exc)
                continue
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            payload = response.json()
            data = payload.get("data") or {}
            raw_children = data.get("children", []) if isinstance(data, dict) else []
            candidates = _parse_listing(payload)
            logger.info(
                "subreddit search done",
                url=url,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
                count_before_filter=len(raw_children),
                count_after_filter=len(candidates),
                phrase=phrase,
            )
            results.append(PhraseResult(phrase=phrase, candidates=candidates))
    finally:
        if own_client:
            await http.aclose()

    if not results and errors:
        raise errors[0]
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/sources/test_reddit_subreddits.py -v`
Expected: PASS — happy path, URL params, empty `ok_empty`, 429-retry, 403-raises, partial success, total-wipeout-raises, limiter routing, per-request logging all pass.

- [ ] **Step 5: Run all checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green. (If ruff flags the appended test imports' `# noqa: E402`, that is intentional — imports follow the appended section header by design; keep the noqa.)

- [ ] **Step 6: Commit**

```
git add src/discovery/sources/reddit_subreddits.py tests/unit/sources/test_reddit_subreddits.py
git commit -m "feat(sources): /subreddits/search client (spec step 2)

Async, shares the process-wide limiter, mirrors reddit.py retry but
401/403 RAISE (spec §11.4 — never empty), partial success across
phrases (skill 17), empty=ok_empty (skill 20), per-request log
(skill 21). Returns PhraseResult DTOs, never Bronze.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Chunk 4: Prompts & schema (spec steps 3–4)

### Task 5: `SubredditSearchPhrases` schema + `subreddit_phrases` prompt (v1)

LLM Call #1: industry → semantic *search phrases* (NOT subreddit names). Output is a station output, so the schema belongs in `schemas.py` (spec §6, llm-station file-layout rule).

**Files:**
- Modify: `src/discovery/llm/schemas.py` (add `SubredditSearchPhrases`)
- Create: `src/discovery/llm/prompts/subreddit_phrases.py`
- Modify: `tests/unit/llm/test_schemas.py` (append `SubredditSearchPhrases` tests)
- Test: `tests/unit/llm/test_prompts_subreddit_phrases.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/llm/test_schemas.py`:

```python
class TestSubredditSearchPhrases:
    def test_accepts_three_to_eight_phrases(self) -> None:
        from discovery.llm.schemas import SubredditSearchPhrases

        m = SubredditSearchPhrases(phrases=["a", "b", "c"])
        assert m.phrases == ["a", "b", "c"]

    def test_rejects_fewer_than_three(self) -> None:
        from discovery.llm.schemas import SubredditSearchPhrases

        with pytest.raises(ValidationError):
            SubredditSearchPhrases(phrases=["a", "b"])

    def test_rejects_more_than_eight(self) -> None:
        from discovery.llm.schemas import SubredditSearchPhrases

        with pytest.raises(ValidationError):
            SubredditSearchPhrases(phrases=[str(i) for i in range(9)])
```

Create `tests/unit/llm/test_prompts_subreddit_phrases.py`:

```python
"""Shape tests for the Call #1 prompt (spec §6 prompt #1). Pins the
module shape, not the wording (wording evolves via VERSION bumps).
"""

from __future__ import annotations

from datetime import date

from discovery.jobs import JobSpec
from discovery.llm.prompts import subreddit_phrases as sp


class TestPromptModule:
    def test_has_version_v1(self) -> None:
        assert sp.VERSION == "v1"

    def test_system_prompt_says_phrases_not_names(self) -> None:
        s = sp.SYSTEM_PROMPT.lower()
        assert "phrase" in s
        assert "subreddit" in s
        # the core rule: search phrases, NOT subreddit names
        assert "not" in s and "name" in s

    def test_few_shot_examples_present_with_phrases(self) -> None:
        assert len(sp.FEW_SHOT_EXAMPLES) >= 2
        for ex in sp.FEW_SHOT_EXAMPLES:
            assert "input" in ex
            assert "output" in ex
            assert "phrases" in ex["output"]
            assert len(ex["output"]["phrases"]) >= 3


class TestBuildUserMessage:
    def test_renders_industry_and_optionals(self) -> None:
        msg = sp.build_user_message(
            JobSpec(
                industry="commercial cleaning",
                as_of=date(2026, 6, 1),
                location="NY",
                size="medium",
            )
        )
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg

    def test_omits_unset_optionals(self) -> None:
        msg = sp.build_user_message(JobSpec(industry="bakery", as_of=date(2026, 6, 1)))
        assert "bakery" in msg
        assert "None" not in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/llm/test_prompts_subreddit_phrases.py tests/unit/llm/test_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discovery.llm.prompts.subreddit_phrases'` and `ImportError: cannot import name 'SubredditSearchPhrases'`.

- [ ] **Step 3: Add the schema**

In `src/discovery/llm/schemas.py`, extend the module docstring's first paragraph to note the second Wave-0 output (keep the existing `NOTE TO FUTURE SESSIONS` block unchanged) — change:

```python
One model per station's output, plus shared sub-models. The output of
the Wave 0 (Query Expansion) station is `JobPlan`.
```
to:
```python
One model per station's output, plus shared sub-models. Wave 0 has two
LLM calls: Call #1 emits `SubredditSearchPhrases` (intermediate — NOT
cached separately, see spec §8) and Call #2 emits `JobPlan` (the
station's final, cached output).
```

Append at the end of `src/discovery/llm/schemas.py`:

```python
class SubredditSearchPhrases(BaseModel):
    """Wave 0 LLM Call #1 output: semantic phrases to SEARCH Reddit's
    subreddit index with — NOT subreddit names (spec §6 prompt #1).
    Frozen for parity with the other station outputs.
    """

    model_config = ConfigDict(frozen=True)

    phrases: list[str] = Field(min_length=3, max_length=8)
```

(`BaseModel`, `ConfigDict`, `Field` are already imported in this file.)

- [ ] **Step 4: Create the prompt module**

Create `src/discovery/llm/prompts/subreddit_phrases.py`:

```python
"""System prompt + helpers for Wave 0 LLM Call #1 (subreddit phrases).

The LLM (OpenAI gpt-5.4) sees this prompt plus a rendered JobSpec and
returns a `SubredditSearchPhrases` (validated in
`discovery.llm.schemas`). These phrases are fed to Reddit's
`/subreddits/search.json` to find REAL, currently-existing subreddits —
the LLM never names subreddits from memory (that was the bug this
feature fixes; see spec §1).

Bumping VERSION
---------------
Bump when the system prompt, few-shot, or intended schema changes. The
combined Wave 0 cache key includes this VERSION (spec §8); bumping it
forces a full fresh re-run (re-phrase, re-search, re-select).

Versioning:
    v1 — initial release. ~5 semantic subreddit-search phrases.
"""

from __future__ import annotations

from typing import Any

from discovery.jobs import JobSpec

VERSION: str = "v1"


SYSTEM_PROMPT: str = """\
You generate SEARCH PHRASES used to discover Reddit communities. You do
NOT name subreddits.

The phrases you return are fed verbatim to Reddit's subreddit-search
index (`/subreddits/search`). Reddit matches them against subreddit
names AND descriptions and returns real, currently-existing
communities. Your job is to maximize the chance that the practitioners,
customers, and adjacent niches of the given industry are surfaced.

# Critical rule

- Output SEARCH PHRASES, NOT subreddit names. `"dog grooming"` is a
  good phrase. `r/doggrooming` is a subreddit NAME — never output that.
  You cannot know which subreddits exist; that is exactly what the
  search step is for.

# Vary the angle

Produce a small set of distinct phrases that approach the industry from
different directions, so the search surfaces a broad, non-redundant set
of communities:

- the trade/practice itself (what insiders call the work)
- practitioner slang or role names
- the customer / buyer side of the same industry
- adjacent or upstream/downstream verticals

# Length

Keep each phrase short (a few words). Reddit's subreddit-search query is
short; long phrases hurt recall. Around 5 phrases is the sweet spot —
enough angles without burning the shared Reddit rate budget.

# Output

A JSON object validated as `SubredditSearchPhrases` with one field:
`phrases` — between 3 and 8 short search phrases. No subreddit names,
no `r/` prefixes, no operators.
"""


FEW_SHOT_EXAMPLES: list[dict[str, Any]] = [
    {
        "input": {"industry": "commercial cleaning", "location": "NY", "size": "medium"},
        "output": {
            "phrases": [
                "commercial cleaning",
                "janitorial services",
                "office cleaning business",
                "facilities maintenance",
                "small business owners",
            ]
        },
    },
    {
        "input": {"industry": "indie game development"},
        "output": {
            "phrases": [
                "indie game development",
                "game dev",
                "solo game developer",
                "game design",
                "game marketing",
            ]
        },
    },
]


def build_user_message(spec: JobSpec) -> str:
    """Render the JobSpec into the Call #1 user message. Only set fields
    are included (location/size are optional).
    """
    lines: list[str] = [f"Industry: {spec.industry}"]
    if spec.location is not None:
        lines.append(f"Location: {spec.location}")
    if spec.size is not None:
        lines.append(f"Company size: {spec.size}")
    lines.append("")
    lines.append(
        "Produce 3-8 short subreddit-SEARCH phrases for this industry. "
        "Phrases that find communities — never subreddit names."
    )
    return "\n".join(lines)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/llm/test_prompts_subreddit_phrases.py tests/unit/llm/test_schemas.py -v`
Expected: PASS — schema bounds (3..8) and prompt shape tests pass.

- [ ] **Step 6: Run all checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green.

- [ ] **Step 7: Commit**

```
git add src/discovery/llm/schemas.py src/discovery/llm/prompts/subreddit_phrases.py tests/unit/llm/test_schemas.py tests/unit/llm/test_prompts_subreddit_phrases.py
git commit -m "feat(llm): Call #1 prompt + SubredditSearchPhrases schema (v1)

LLM emits semantic subreddit-SEARCH phrases (not names) — the core
move that kills hallucinated/stale subreddits (spec §1, §6). Schema
bounded 3..8 phrases.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 6: `query_expansion` prompt v3→v4 — grounding + table signature

Call #2 keeps every v3 content-query rule and adds the grounding section. `build_user_message` signature changes to take the candidate table.

**Files:**
- Modify: `src/discovery/llm/prompts/query_expansion.py` (VERSION, grounding section, builder signature, docstring)
- Modify: `tests/unit/llm/test_prompts_query_expansion.py` (new signature, grounding substrings, v4)

- [ ] **Step 1: Update the prompt shape tests first (they must fail)**

Replace the body of `tests/unit/llm/test_prompts_query_expansion.py` with:

```python
"""Shape tests for the Wave 0 Call #2 prompt (query_expansion v4).

Pins module shape, not wording. v4 adds the grounding section and the
table-bearing build_user_message signature.
"""

from __future__ import annotations

from datetime import date

from discovery.jobs import JobSpec
from discovery.llm.prompts import query_expansion as qe
from discovery.sources.reddit_subreddits import SubredditCandidate


def _table() -> list[SubredditCandidate]:
    return [
        SubredditCandidate(
            name="CleaningTips",
            subscribers=12000,
            active_user_count=80,
            activity_ratio=0.0067,
            public_description="tips for cleaning professionals",
            matched_phrases=3,
        )
    ]


class TestPromptModule:
    def test_version_is_v4(self) -> None:
        assert qe.VERSION == "v4"

    def test_system_prompt_keeps_core_v3_rules(self) -> None:
        sp = qe.SYSTEM_PROMPT
        assert "OR" in sp
        assert "subreddit:" in sp
        assert "quote" in sp.lower() or "quoted" in sp.lower()
        assert "rationale" in sp.lower()
        assert "10" in sp and "15" in sp

    def test_system_prompt_has_grounding_section(self) -> None:
        s = qe.SYSTEM_PROMPT.lower()
        # the non-negotiable grounding rule + how-to-read-the-table guide
        assert "only" in s and "table" in s
        assert "never" in s and ("invent" in s or "memory" in s)
        assert "matched_phrases" in s
        assert "public_description" in s
        assert "activity_ratio" in s

    def test_few_shot_examples_still_present(self) -> None:
        assert len(qe.FEW_SHOT_EXAMPLES) >= 2
        for ex in qe.FEW_SHOT_EXAMPLES:
            assert len(ex["output"]["reddit_queries"]) >= 10


class TestBuildUserMessage:
    def test_renders_spec_and_table(self) -> None:
        msg = qe.build_user_message(
            JobSpec(
                industry="commercial cleaning",
                as_of=date(2026, 6, 1),
                location="NY",
                size="medium",
            ),
            _table(),
        )
        assert "commercial cleaning" in msg
        assert "NY" in msg
        assert "medium" in msg
        assert "2026-06-01" in msg
        # the rendered table must be embedded
        assert "CleaningTips" in msg
        assert "matched_phrases" in msg  # the table header

    def test_handles_optional_fields(self) -> None:
        msg = qe.build_user_message(
            JobSpec(industry="bakery", as_of=date(2026, 6, 1)), _table()
        )
        assert "bakery" in msg
        assert "None" not in msg

    def test_includes_time_window(self) -> None:
        msg = qe.build_user_message(
            JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year"),
            _table(),
        )
        assert "year" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v`
Expected: FAIL — `qe.VERSION == "v4"` fails (still "v3"); `build_user_message` raises `TypeError` (takes 1 arg, 2 given); grounding substrings absent.

- [ ] **Step 3: Bump VERSION and add the versioning note**

In `src/discovery/llm/prompts/query_expansion.py`:

Change `VERSION: str = "v3"` to `VERSION: str = "v4"`.

In the module docstring's `Versioning:` block, append after the `v3` entry:

```python
    v4 — grounded selection. The LLM no longer recalls subreddit names
    from memory; it selects exclusively from a supplied table of REAL
    subreddits (spec §6 prompt #2). build_user_message now takes that
    table. All v3 content-query rules are retained unchanged.
```

- [ ] **Step 4: Add the grounding section to `SYSTEM_PROMPT`**

In `src/discovery/llm/prompts/query_expansion.py`, insert the following block into `SYSTEM_PROMPT` immediately **before** the `# What NOT to do` section (keep every existing rule above it verbatim):

```text
# GROUNDING — you may ONLY use the supplied subreddit table

A table of REAL, currently-existing subreddits for THIS job is included
in the user message (columns: name, subscribers, active_user_count,
activity_ratio, public_description, matched_phrases).

Hard rule: these are the ONLY subreddits available for this job. Select
exclusively from this table. Never use a subreddit that is not listed.
Never invent names. Do NOT fall back to your own knowledge or memory.
If the table is thin, use FEWER distinct subreddits — but you must
STILL produce 10-15 content queries by varying the pain-phrase angle
across the available subs (per_sub and site_wide combinations). Do NOT
emit fewer than 10 queries. Query count is driven by subreddit ×
pain-category combinations, not 1:1 with subreddit count — even 3 subs
comfortably yield 10-15 queries.

How to read the table (and its traps):

- `public_description` is the PRIMARY relevance signal. Does the sub's
  stated purpose match the industry, or does it merely contain the
  word? A generic giant that happens to mention the term is noise.
- `matched_phrases` high ⇒ robustly on-topic. `matched_phrases = 1` ⇒
  likely a fluke single-phrase description match; treat with suspicion.
- `activity_ratio` is misleading on tiny subs — always cross-check the
  raw `active_user_count` (12 active people is thin regardless of
  ratio).
- Large `subscribers` is NOT better. Prefer a focused practitioner
  community over a generic mega-sub.

Selection instruction: keep every subreddit that is clearly on-topic
AND alive. ORDER your selection best→worst by your own confidence.
There is no minimum. The hard ceiling is 30 — if you return more than
30, only your first 30 (in your order) are kept.
```

- [ ] **Step 5: Change the `build_user_message` signature to take the table**

In `src/discovery/llm/prompts/query_expansion.py`, add to the import block:

```python
from discovery.sources.reddit_subreddits import (
    SubredditCandidate,
    render_candidate_table,
)
```

Replace the entire `build_user_message` function with:

```python
def build_user_message(spec: JobSpec, table: list[SubredditCandidate]) -> str:
    """Render the JobSpec plus the grounded subreddit table into the
    Call #2 user message.

    `table` is the deterministic pipeline's surviving candidates. It is
    rendered compactly via `render_candidate_table` (the 6 LLM-facing
    columns — spec §5); the LLM may pick subreddits ONLY from it.
    Optional spec fields are included only when set. `time_window` is
    the user-chosen search depth — every query's `t` is later forced to
    match it deterministically (skill item 11).
    """
    lines: list[str] = [f"Industry: {spec.industry}"]
    lines.append(f"As of: {spec.as_of.isoformat()}")
    if spec.location is not None:
        lines.append(f"Location: {spec.location}")
    if spec.size is not None:
        lines.append(f"Company size: {spec.size}")
    lines.append(f"Search time window (Reddit `t`): {spec.time_window}")
    lines.append("")
    lines.append(
        "Subreddit table (select EXCLUSIVELY from these — never invent "
        "names, never use memory):"
    )
    lines.append(render_candidate_table(table))
    lines.append("")
    lines.append(
        "Produce a JobPlan with 10-15 reddit_queries using ONLY the "
        "subreddits above. Follow the system-prompt rules; explain each "
        "query's rationale."
    )
    return "\n".join(lines)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/llm/test_prompts_query_expansion.py -v`
Expected: PASS — v4, grounding substrings present, table rendered into the message, optionals handled.

- [ ] **Step 7: Run all checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: pytest will FAIL only in `tests/unit/llm/stations/test_query_expansion.py` (its cache-hit test still builds the key with `prompt_version=qe.VERSION` and its stub returns a JobPlan for a now-two-call flow). **That is expected and is fixed in Task 7** — but `-x` stops at the first failure. To keep this task self-contained, run the scoped check instead and defer the full suite:

Run: `uv run pytest tests/unit/llm tests/unit/sources --ignore=tests/unit/llm/stations/test_query_expansion.py && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green. (The station test is intentionally left red until Task 7 wires the station; do not "fix" it here.)

- [ ] **Step 8: Commit**

```
git add src/discovery/llm/prompts/query_expansion.py tests/unit/llm/test_prompts_query_expansion.py
git commit -m "feat(llm): query_expansion prompt v4 — grounded selection

Adds the GROUNDING section (select ONLY from the supplied real-sub
table, never memory; how to read the columns + traps; best→worst
order; ceiling 30). build_user_message now takes the candidate table
and embeds the rendered 6-column projection. All v3 content-query
rules retained. Station wiring + station tests land next task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Chunk 5: Station wiring + smoke + handoff (spec steps 5–6)

### Task 7: Wire the two LLM calls + deterministic middle into `run_query_expansion`

The integration. Public signature `run_query_expansion(spec) -> JobPlan` is unchanged; internally it becomes Call #1 → client → deterministic middle → Call #2 → off-table reject + overflow trim → the **unchanged** existing tail. One combined cache entry. Every failure path in spec §10 raises `QueryExpansionError`.

**No standalone subreddit-count floor.** Spec §10's row "LLM picks off-table subs → reject those subs; if too few remain → QueryExpansionError" is realized through the *existing* query-validation floor (`_drop_invalid_queries` → `MIN_VALID_QUERIES`), NOT a post-reject subreddit count. Locked decision §2.4 forbids a subreddit minimum (selection is adaptive — "no minimum"). Do **not** add a `len(selected) < N` gate in `_ground_selection`.

**Files:**
- Modify: `src/discovery/llm/stations/query_expansion.py`
- Modify: `tests/unit/llm/stations/test_query_expansion.py` (rewrite for the two-call flow + §10 table)

- [ ] **Step 1: Rewrite the station test file (it must fail against the old station)**

Replace the entire contents of `tests/unit/llm/stations/test_query_expansion.py` with:

```python
"""Tests for `run_query_expansion` — the integrated Wave 0 flow.

Never calls real OpenAI or real Reddit. We monkeypatch BOTH:
  - `station.call_openai` — a dispatcher returning SubredditSearchPhrases
    for Call #1 and a JobPlan for Call #2 (keyed on `response_model`).
  - `station.search_subreddits` — a fake returning canned PhraseResults.
Plus the diskcache is pointed at a temp dir per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from diskcache import Cache

from discovery.jobs import JobSpec
from discovery.llm.cache import cache_key, make_cache, put_cached
from discovery.llm.prompts import query_expansion as qe
from discovery.llm.prompts import subreddit_phrases as sp
from discovery.llm.schemas import JobPlan, RedditQuerySpec, SubredditSearchPhrases
from discovery.llm.stations import query_expansion as station
from discovery.llm.stations.query_expansion import (
    MODEL,
    QueryExpansionError,
    run_query_expansion,
)
from discovery.sources.reddit_subreddits import PhraseResult, SubredditCandidate

# --- shared fakes --------------------------------------------------------


def _query(label: str = "x", sub: str = "startups") -> RedditQuerySpec:
    return RedditQuerySpec(
        endpoint="site_wide",
        q=f'(subreddit:{sub}) AND "{label}"',
        rationale=label,
    )


def _plan(subs: list[str] | None = None, n: int = 10) -> JobPlan:
    return JobPlan(
        reddit_queries=[_query(f"q{i}") for i in range(n)],
        reddit_subreddits=subs if subs is not None else ["startups"],
    )


def _candidates(*names: str) -> list[SubredditCandidate]:
    return [
        SubredditCandidate(
            name=n,
            subscribers=5000,
            active_user_count=120,
            subreddit_type="public",
            public_description=f"{n} practitioners",
        )
        for n in (names or ("startups",))
    ]


def _make_call_openai(
    plan: JobPlan,
    phrases: SubredditSearchPhrases | None = None,
) -> Any:
    async def _call(**kwargs: Any) -> Any:
        if kwargs["response_model"] is SubredditSearchPhrases:
            return phrases or SubredditSearchPhrases(phrases=["a", "b", "c"])
        return plan

    return _call


def _make_search(*names: str) -> Any:
    async def _search(phrases: list[str], **kwargs: Any) -> list[PhraseResult]:
        return [PhraseResult(phrase=p, candidates=_candidates(*names)) for p in phrases]

    return _search


@pytest.fixture
def tmp_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cache]:
    cache = make_cache(tmp_path / "cache")
    monkeypatch.setattr(station, "_cache", cache)
    yield cache
    cache.close()


@pytest.fixture
def spec() -> JobSpec:
    return JobSpec(industry="commercial cleaning", as_of=date(2026, 6, 1))


def _combined_key(spec: JobSpec) -> str:
    return cache_key(
        spec=spec.model_dump(mode="json"),
        prompt_version=f"{sp.VERSION}+{qe.VERSION}",
        model=MODEL,
    )


class TestCacheHit:
    async def test_cache_hit_skips_both_calls_and_the_client(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        put_cached(tmp_cache, _combined_key(spec), _plan())

        async def _explode_llm(**kwargs: Any) -> None:
            raise AssertionError("no LLM call on cache hit")

        async def _explode_search(*a: Any, **k: Any) -> None:
            raise AssertionError("no sub-search on cache hit")

        monkeypatch.setattr(station, "call_openai", _explode_llm)
        monkeypatch.setattr(station, "search_subreddits", _explode_search)

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)
        assert len(result.reddit_queries) == 10


class TestCacheMiss:
    async def test_runs_full_chain_then_caches(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(station, "call_openai", _make_call_openai(_plan()))
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))

        result = await run_query_expansion(spec)
        assert isinstance(result, JobPlan)

        async def _explode(**kwargs: Any) -> None:
            raise AssertionError("expected cache hit on second call")

        monkeypatch.setattr(station, "call_openai", _explode)
        again = await run_query_expansion(spec)
        assert len(again.reddit_queries) == len(result.reddit_queries)


class TestValidationDropsInvalidQueries:
    async def test_drops_lowercase_or_query_via_existing_tail(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = [_query(f"g{i}") for i in range(10)]
        bad = RedditQuerySpec(
            endpoint="site_wide", q='(subreddit:a or subreddit:b) AND "x"', rationale="b"
        )
        monkeypatch.setattr(
            station,
            "call_openai",
            _make_call_openai(JobPlan(reddit_queries=[*good, bad])),
        )
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        result = await run_query_expansion(spec)
        assert len(result.reddit_queries) == 10  # bad dropped by existing tail


class TestFallbackTable:
    """Every row of spec §10's failure table → QueryExpansionError."""

    async def test_call1_failure(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _call(**kwargs: Any) -> Any:
            raise RuntimeError("call #1 down")

        monkeypatch.setattr(station, "call_openai", _call)
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_all_phrase_searches_fail(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _search(phrases: list[str], **kwargs: Any) -> list[PhraseResult]:
            raise RuntimeError("reddit down")

        monkeypatch.setattr(station, "call_openai", _make_call_openai(_plan()))
        monkeypatch.setattr(station, "search_subreddits", _search)
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_zero_subs_survive_filtering(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _search(phrases: list[str], **kwargs: Any) -> list[PhraseResult]:
            # all private → drop_non_public wipes them out
            return [
                PhraseResult(
                    phrase=p,
                    candidates=[
                        SubredditCandidate(name="ghost", subreddit_type="private")
                    ],
                )
                for p in phrases
            ]

        monkeypatch.setattr(station, "call_openai", _make_call_openai(_plan()))
        monkeypatch.setattr(station, "search_subreddits", _search)
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_call2_failure(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _call(**kwargs: Any) -> Any:
            if kwargs["response_model"] is SubredditSearchPhrases:
                return SubredditSearchPhrases(phrases=["a", "b", "c"])
            raise RuntimeError("call #2 down")

        monkeypatch.setattr(station, "call_openai", _call)
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)

    async def test_too_few_valid_queries_after_tail(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = [_query(f"g{i}") for i in range(9)]
        bad = RedditQuerySpec(
            endpoint="site_wide", q='(subreddit:a or subreddit:b)', rationale="b"
        )
        monkeypatch.setattr(
            station,
            "call_openai",
            _make_call_openai(JobPlan(reddit_queries=[*good, *[bad] * 6])),
        )
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        with pytest.raises(QueryExpansionError):
            await run_query_expansion(spec)


class TestOffTableRejection:
    async def test_off_table_subs_are_stripped_from_selection(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # LLM Call #2 picks "ghost" which is NOT in the table.
        plan = _plan(subs=["startups", "ghost"])
        monkeypatch.setattr(station, "call_openai", _make_call_openai(plan))
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        result = await run_query_expansion(spec)
        assert "ghost" not in result.reddit_subreddits
        assert "startups" in result.reddit_subreddits


class TestTimeWindowOverride:
    async def test_forces_window_on_every_query(
        self, tmp_cache: Cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        year_spec = JobSpec(industry="x", as_of=date(2026, 6, 1), time_window="year")
        mixed = [
            RedditQuerySpec(
                endpoint="site_wide",
                q=f'(subreddit:startups) AND "q{i}"',
                rationale=f"r{i}",
                t=("month" if i % 2 else "week"),  # type: ignore[arg-type]
            )
            for i in range(10)
        ]
        monkeypatch.setattr(
            station, "call_openai", _make_call_openai(JobPlan(reddit_queries=mixed))
        )
        monkeypatch.setattr(station, "search_subreddits", _make_search("startups"))
        result = await run_query_expansion(year_spec)
        assert {q.t for q in result.reddit_queries} == {"year"}


class TestBaselineSubredditMerge:
    async def test_baseline_appended_after_on_table_picks(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Table contains the domain subs the LLM picks, so reject_off_table
        # keeps them; baseline (startups/microsaas/smallbusiness) is then
        # appended by the unchanged tail.
        plan = _plan(subs=["doggrooming", "groomers", "petbusiness"])
        monkeypatch.setattr(station, "call_openai", _make_call_openai(plan))
        monkeypatch.setattr(
            station,
            "search_subreddits",
            _make_search("doggrooming", "groomers", "petbusiness"),
        )
        result = await run_query_expansion(spec)
        for baseline in ("startups", "microsaas", "smallbusiness"):
            assert baseline in result.reddit_subreddits
        assert result.reddit_subreddits[:3] == [
            "doggrooming",
            "groomers",
            "petbusiness",
        ]

    async def test_no_duplicate_when_llm_picked_a_baseline(
        self, tmp_cache: Cache, spec: JobSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan = _plan(subs=["smallbusiness", "Entrepreneur", "wholesale"])
        monkeypatch.setattr(station, "call_openai", _make_call_openai(plan))
        monkeypatch.setattr(
            station,
            "search_subreddits",
            _make_search("smallbusiness", "Entrepreneur", "wholesale"),
        )
        result = await run_query_expansion(spec)
        assert result.reddit_subreddits.count("smallbusiness") == 1
        assert "startups" in result.reddit_subreddits
        assert "microsaas" in result.reddit_subreddits
```

- [ ] **Step 2: Run the station tests to verify they fail**

Run: `uv run pytest tests/unit/llm/stations/test_query_expansion.py -v`
Expected: FAIL — `ImportError` for `search_subreddits` not present in `station` namespace (and the old single-call flow can't satisfy the two-call fakes).

- [ ] **Step 3: Rewrite the station to integrate the flow**

Replace the entire contents of `src/discovery/llm/stations/query_expansion.py` with:

```python
"""Wave 0 — Query Expansion station (grounded subreddit discovery).

Public entry `run_query_expansion(spec) -> JobPlan` is UNCHANGED.
Internally it is now a multi-step process (spec §3):

    1. Combined cache key over (spec, sp.VERSION+qe.VERSION, model).
       Cache hit → return cached JobPlan (skips everything below).
    2. LLM Call #1 (subreddit_phrases) → semantic search phrases.
    3. Reddit /subreddits/search per phrase → SubredditCandidate DTOs.
    4. Deterministic middle (no LLM): dedupe+consensus → drop
       non-public → drop NSFW → median → drop drastically-below-median
       → activity_ratio.
    5. LLM Call #2 (query_expansion v4) → JobPlan: selects ONLY from the
       supplied table and designs the 10-15 content queries.
    6. Defensive off-table reject + overflow trim (≤30, LLM order).
    7. EXISTING deterministic tail, UNCHANGED and order-preserved:
       _drop_invalid_queries → MIN_VALID_QUERIES → _force_time_window
       → _merge_baseline_subreddits.
    8. Cache the final JobPlan under the combined key.

Any failure raises `QueryExpansionError`; `plan_job` already catches it
and the Reddit orchestrator falls back to the deterministic template
(spec §10 — no new fallback branches).

Temperature 0.2 (not the skill default 0): Call #2 brainstorms creative
query designs; Call #1 brainstorms phrases. Documented in
`.claude/skills/llm-station/SKILL.md`'s per-station deviation table.
"""

from __future__ import annotations

from loguru import logger

from discovery.config.settings import settings
from discovery.jobs import JobSpec
from discovery.llm.cache import cache_key, get_cached, make_cache, put_cached
from discovery.llm.client import call_openai
from discovery.llm.prompts import query_expansion, subreddit_phrases
from discovery.llm.schemas import JobPlan, RedditQuerySpec, SubredditSearchPhrases
from discovery.llm.stations.subreddit_selection import (
    dedupe_and_count,
    drop_below_median,
    drop_non_public,
    drop_nsfw,
    reject_off_table,
    subscriber_median,
    trim_overflow,
    with_activity_ratio,
)
from discovery.orchestrator.reddit_query_validator import validate_reddit_query
from discovery.sources.reddit_subreddits import (
    PhraseResult,
    SubredditCandidate,
    search_subreddits,
)

MODEL: str = "gpt-5.4"
TEMPERATURE: float = 0.2
MIN_VALID_QUERIES: int = 10

# Skill item 9 — profile-agnostic baseline subreddits merged into every
# JobPlan as defense in depth (independent of discovery — spec §13).
_BASELINE_SUBREDDITS: tuple[str, ...] = ("startups", "microsaas", "smallbusiness")


class QueryExpansionError(Exception):
    """Raised when the station can't produce a valid JobPlan."""


_cache = make_cache(settings.llm_cache_dir)


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

    grounded = _ground_selection(raw_plan, candidates)
    final_plan = _finalize(grounded, spec)
    put_cached(_cache, key, final_plan)
    return final_plan


async def _generate_phrases(spec: JobSpec) -> SubredditSearchPhrases:
    """LLM Call #1 — semantic subreddit-search phrases (spec §6 #1)."""
    try:
        return await call_openai(
            system=subreddit_phrases.SYSTEM_PROMPT,
            user=subreddit_phrases.build_user_message(spec),
            response_model=SubredditSearchPhrases,
            model=MODEL,
            temperature=TEMPERATURE,
        )
    except Exception as e:
        raise QueryExpansionError(
            f"phrase generation failed: {type(e).__name__}: {e}"
        ) from e


async def _discover_subreddits(
    phrases: SubredditSearchPhrases,
) -> list[SubredditCandidate]:
    """Reddit sub-search + the deterministic middle (spec §7 steps 1-7).

    Raises `QueryExpansionError` on total search wipeout or when nothing
    survives filtering.
    """
    try:
        results: list[PhraseResult] = await search_subreddits(
            list(phrases.phrases),
            user_agent=settings.reddit_user_agent,
        )
    except Exception as e:
        raise QueryExpansionError(
            f"subreddit search failed: {type(e).__name__}: {e}"
        ) from e

    candidates = dedupe_and_count(results)
    candidates = drop_non_public(candidates)
    candidates = drop_nsfw(candidates)
    if not candidates:
        raise QueryExpansionError("no public subreddits surfaced for any phrase")

    median = subscriber_median(candidates)
    candidates = drop_below_median(candidates, median)
    candidates = with_activity_ratio(candidates)
    if not candidates:
        raise QueryExpansionError("all candidates dropped by the median floor")

    logger.info(
        "subreddit discovery: {} candidates survived (median subs={})",
        len(candidates),
        median,
    )
    return candidates


async def _select_and_design(
    spec: JobSpec, candidates: list[SubredditCandidate]
) -> JobPlan:
    """LLM Call #2 — grounded selection + query design (spec §6 #2)."""
    try:
        return await call_openai(
            system=query_expansion.SYSTEM_PROMPT,
            user=query_expansion.build_user_message(spec, candidates),
            response_model=JobPlan,
            model=MODEL,
            temperature=TEMPERATURE,
        )
    except Exception as e:
        raise QueryExpansionError(
            f"selection/query design failed: {type(e).__name__}: {e}"
        ) from e


def _ground_selection(plan: JobPlan, candidates: list[SubredditCandidate]) -> JobPlan:
    """Spec §7 step 9 + §10 defensive filter: drop off-table picks, then
    keep the LLM's first 30 in its emitted order.
    """
    selected = reject_off_table(list(plan.reddit_subreddits), candidates)
    selected = trim_overflow(selected)
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=selected,
    )


def _finalize(plan: JobPlan, spec: JobSpec) -> JobPlan:
    """The EXISTING deterministic tail, UNCHANGED and order-preserved
    (spec §7 step 10): drop invalid queries → MIN_VALID_QUERIES check →
    force time window → merge baseline subs.
    """
    filtered_plan = _drop_invalid_queries(plan)
    if len(filtered_plan.reddit_queries) < MIN_VALID_QUERIES:
        raise QueryExpansionError(
            f"Only {len(filtered_plan.reddit_queries)} of "
            f"{len(plan.reddit_queries)} queries passed validation; "
            f"need at least {MIN_VALID_QUERIES}."
        )
    aligned_plan = _force_time_window(filtered_plan, spec.time_window)
    return _merge_baseline_subreddits(aligned_plan)


def _force_time_window(plan: JobPlan, time_window: str) -> JobPlan:
    """Override every query's `t` to the user's chosen window (skill
    item 11) — deterministic, even if the LLM picked differently.
    """
    new_queries = [q.model_copy(update={"t": time_window}) for q in plan.reddit_queries]
    return JobPlan.model_construct(
        reddit_queries=new_queries,
        reddit_subreddits=plan.reddit_subreddits,
    )


def _merge_baseline_subreddits(plan: JobPlan) -> JobPlan:
    """Append the skill's baseline subs (item 9) after the LLM picks;
    no duplicates; LLM order preserved at the front.
    """
    merged = list(plan.reddit_subreddits)
    seen = set(merged)
    for sub in _BASELINE_SUBREDDITS:
        if sub not in seen:
            merged.append(sub)
            seen.add(sub)
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=merged,
    )


def _drop_invalid_queries(plan: JobPlan) -> JobPlan:
    """Keep only queries that pass `validate_reddit_query`. Uses
    `model_construct` so the result skips the `min_length=10` check —
    the caller handles the "too few survived" case.
    """
    kept: list[RedditQuerySpec] = []
    for q in plan.reddit_queries:
        errors = validate_reddit_query(q)
        if errors:
            logger.warning("dropping invalid LLM query: errors={} q={!r}", errors, q.q)
            continue
        kept.append(q)
    return JobPlan.model_construct(
        reddit_queries=kept,
        reddit_subreddits=plan.reddit_subreddits,
    )
```

- [ ] **Step 4: Run the station tests to verify they pass**

Run: `uv run pytest tests/unit/llm/stations/test_query_expansion.py -v`
Expected: PASS — cache-hit skips everything, full chain + caching, validation drop, every §10 fallback row raises, off-table rejection, time-window override, baseline merge (incl. no-duplicate).

- [ ] **Step 5: Run the FULL checks (the suite is whole again)**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/`
Expected: all green — full suite (baseline ~165 + the new tests) passes; `test_orchestrator_jobs.py` is green untouched (it stubs `run_query_expansion(spec)->JobPlan`; signature unchanged). If `ruff format --check` flags files run `uv run ruff format .` and re-stage.

- [ ] **Step 6: Commit**

```
git add src/discovery/llm/stations/query_expansion.py tests/unit/llm/stations/test_query_expansion.py
git commit -m "feat(llm): wire grounded subreddit discovery into Wave 0 (spec step 5)

run_query_expansion now: Call #1 (phrases) -> /subreddits/search ->
deterministic middle -> Call #2 (grounded selection+design) ->
off-table reject + overflow trim -> UNCHANGED tail. One combined cache
entry (sp.VERSION+qe.VERSION). Every spec §10 failure -> the proven
QueryExpansionError template fallback. Public signature unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

### Task 8: Manual smoke + handoff update (spec step 6)

Verify the integrated chain against real OpenAI + real Reddit on one niche and one rich industry, read the Item-21 logs, then update the running handoff log.

**Files:**
- Modify: `docs/handoff.md`

- [ ] **Step 1: Pre-checks — DB ready and OPENAI_API_KEY resolvable**

Run: `uv run python -m discovery.cli.init_db`
Expected: `alembic upgrade head` completes (or "already at head").

Then confirm the OpenAI key resolves (settings shares the main project's `.env` even from a worktree):
Run (PowerShell): `uv run python -c "from discovery.config.settings import settings; print('OPENAI set:', settings.openai_api_key is not None)"`
Expected: `OPENAI set: True`. If `False`, STOP — a missing key surfaces as `wave 0: fallback` (a `RuntimeError` from `_get_openai_client`), which must NOT be misdiagnosed as a discovery-logic failure. Fix the key before smoking.

- [ ] **Step 2: Smoke — rich industry**

Run (PowerShell): `$env:PYTHONIOENCODING="utf-8"; uv run discovery run --industry "food truck" --location US`
Expected: `wave 0: planned` (NOT `fallback`); a non-empty `queued task` line with `queries=` between 10 and 15; Reddit pulls ≥1 post into `raw_records`. In the logs you should see `subreddit search done` lines (one per phrase, skill item 21) with `count_before_filter`/`count_after_filter`, then `subreddit discovery: N candidates survived`, then the existing `reddit query done` content-fetch lines.

If it prints `fallback`: capture the logged `QueryExpansionError` reason and STOP — investigate before continuing (do not silently accept the template path on the smoke).

- [ ] **Step 3: Smoke — niche industry (thin-table path, spec §10)**

Run (PowerShell): `$env:PYTHONIOENCODING="utf-8"; uv run discovery run --industry "mobile dog grooming" --location US --time-window year`
Expected: `wave 0: planned`; still 10-15 queries even if the table is thin (spec §10 — query count is sub×pain-category, not 1:1 with sub count); the `subreddit discovery: N candidates survived` line shows a smaller N than the rich run. Confirm the discovery did NOT auto-fallback purely because the niche table was small.

- [ ] **Step 4: Spot-check the cache is one combined entry**

Run the same rich command again:
Run (PowerShell): `$env:PYTHONIOENCODING="utf-8"; uv run discovery run --industry "food truck" --location US`
Expected: `wave 0: planned` near-instant, a `query_expansion cache hit` debug line, and NO `subreddit search done` lines (the cache hit skipped phrase-gen, all sub-searches, and selection in one shot — spec §8).

- [ ] **Step 5: Update `docs/handoff.md`**

Edit `docs/handoff.md`:
- **`Last touched`** → today's date; **`Branch`** → the current working branch.
- **`What runs end-to-end today`**: note Wave 0 is now grounded subreddit discovery (LLM emits search phrases → real subreddits → deterministic rank → grounded selection), one combined cache entry; the template fallback is unchanged.
- **Commit history table**: prepend the 7 commits from Tasks 1-7 (newest first).
- **Test counts**: replace with the new total from Step `uv run pytest` (record the exact `N passed`).
- **Pieces that exist (the map)**: add `sources/reddit_ratelimit.py`, `sources/reddit_subreddits.py`, `llm/stations/subreddit_selection.py`, `llm/prompts/subreddit_phrases.py`; note `prompts/query_expansion.py` is now v4 and `build_user_message` takes the table.
- **Decisions locked in**: add: shared process-wide Reddit limiter; Wave 0 = two LLM calls + deterministic middle; LLM selects only from the supplied table; combined cache key `sp.VERSION+qe.VERSION`; `MIN_VALID_QUERIES`/`min_length=10` and the deterministic tail deliberately unchanged (spec §10 reconciliation — do not "fix" the floor).
- **Open follow-ups**: add the DRY follow-up — extract a shared `reddit_http` retry helper so `reddit.py._fetch_with_retries` and `reddit_subreddits._get_with_retries` stop duplicating the skill-item-4 policy (deferred; spec-sanctioned mirroring for now). Also note `median/10` divisor + ~5 phrase count are tunable-via-data (spec §13).
- Update the **`Sources for the GPT-5.4 model details`** section only if anything changed (it didn't — leave as-is).

- [ ] **Step 6: Final full checks**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src/ && uv run discovery --help`
Expected: all green; `discovery --help` lists `version`, `hello`, `run` (and any other shipped subcommands).

- [ ] **Step 7: Commit**

```
git add docs/handoff.md
git commit -m "docs(handoff): grounded subreddit discovery shipped (spec step 6)

Updates state, commit table, locked decisions, test count, and the
DRY follow-up (shared reddit_http retry helper). Smoke verified on a
rich and a niche industry; cache confirmed as one combined Wave 0
entry.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Done criteria

- All 8 tasks committed; `uv run pytest` green at a recorded `N passed` ≥ baseline + new tests; `ruff check`, `ruff format --check`, `mypy src/` clean.
- `run_query_expansion(spec) -> JobPlan` signature unchanged; one combined cache entry; existing template fallback path unchanged and exercised by the §10 tests.
- The deterministic tail (`_drop_invalid_queries` → `MIN_VALID_QUERIES` → `_force_time_window` → `_merge_baseline_subreddits`), `JobPlan`/`RedditQuerySpec` shape, and `MIN_VALID_QUERIES=10` are byte-for-byte behavior-unchanged.
- Manual smoke: rich + niche industries both `wave 0: planned` with 10-15 queries; cache re-hit skips phrase-gen/search/selection.
- `docs/handoff.md` reflects reality for the next session.
