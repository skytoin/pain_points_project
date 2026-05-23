# YouTube Source Adapter Implementation Plan

> **For agentic workers:** REQUIRED: Use @superpowers:subagent-driven-development to implement this plan (fresh subagent per task + two-stage review). Steps use checkbox (`- [ ]`) syntax for tracking. The authoritative design is [`docs/specs/2026-05-22-youtube-source-design.md`](../specs/2026-05-22-youtube-source-design.md) — every locked decision in this plan traces back to a section there.

**Goal:** Add YouTube (Data API v3) as the third Wave-1 source so every `discovery run` fans out to Reddit AND HackerNews AND YouTube concurrently, capturing video resources (with view stats) and the comment threads under the highest-view videos verbatim into the existing Bronze layer.

**Architecture:** Approach A — the existing Wave-0 LLM emits `YouTubeQuerySpec` candidates (emotion/pain-shaped search phrase + `intent` + rationale) alongside Reddit/HN outputs in one combined v8 prompt call; Python in a new `orchestrator/youtube.py` owns all mechanics (normalize, dedup, `publishedAfter` from `JobSpec.time_window`, `MAX_YT_QUERIES=10` cap). A new `sources/youtube.py` does a three-step fetch — `search.list` → `videos.list` enrichment → `commentThreads.list` for the top `COMMENT_TOP_K=50` videos by view count — with quota-aware retry (retry transient/rate-limit, hard-stop on `quotaExceeded`). The locked Wave-0 Reddit tail stays Reddit-only via a single **generalized** carry-through helper (`_attach_extra_source_queries`) that re-attaches both `hn_queries` and `youtube_queries`. Parallel three-way dispatch is the HN slice's `cli/run.py` + `claim_known_task` pattern with a third branch.

**Tech Stack:** Python 3.12, async httpx + aiolimiter (per-instance for YouTube), hand-rolled retry with injectable `sleep` (mirroring `RedditSource`, NOT tenacity), Pydantic 2 (frozen + `default_factory=list` permissive), SQLModel/SQLAlchemy async, `instructor` + OpenAI gpt-5.4 (existing Wave-0 station, prompt VERSION v7 → v8), pytest with `httpx.MockTransport`, loguru.

---

## Pre-flight (verify before any code)

- [ ] **Read the spec end-to-end:** [`docs/specs/2026-05-22-youtube-source-design.md`](../specs/2026-05-22-youtube-source-design.md). Locked decisions: §4 (quota), §6 (generalized carry-through), §8 (Kind 4 prompt), §9-10 (orchestrator + 3-step fetch), §16 (divergences), §17 (invariants), §18 (decision record).
- [ ] **Read these project skills:** @source-adapter (umbrella), @hackernews-source (closest sibling — mirror its shape, note where YouTube diverges), @reddit-source (the injectable-sleep retry + limiter patterns to mirror), @llm-station (LLM call contract for the prompt/station tasks).
- [ ] **Verify project health before touching code (run in WSL):**

  ```
  wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run pytest"
  wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run ruff check src tests"
  wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run ruff format --check src tests"
  wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run mypy src/"
  ```

  All four must be green (one known pre-existing WSL-only failure is allowed: `tests/unit/test_settings.py::TestFindProjectRoot::test_windows_style_worktree_path`). If anything else is red, stop and fix before Task 1.1.

- [ ] **Environment notes.** Run all `uv` / `pytest` / `ruff` / `mypy` in WSL: `wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && <cmd>"`. Worktrees are unusable on this checkout (a POSIX `lib64` reparse point breaks `uv` on the worktree `.venv`) — work on the `feat/youtube-source` branch in the MAIN checkout. File edits and git work fine on the Windows side.
- [ ] **Lint landmines:** `ruff check src tests` (not bare `.`). Watch PLC0415 (no imports inside functions/tests — except the deliberate lazy imports in `build_default_registry`, which carry `# noqa: PLC0415`), PT018 (no compound asserts), RUF001 (ASCII-only in new source/prompt strings — no `§`/em-dash in code strings; they're tolerated in docstrings with existing precedent), ASYNC109 (do NOT name an async param `timeout` — it's fine as a sync constructor kwarg, as in Reddit/HN).
- [ ] **pytest config:** `asyncio_mode=auto` (bare `async def test_`, no decorator), `filterwarnings=["error"]` (an unclosed httpx client FAILS the test — every adapter test must close its client; use `session.exec()` not `session.execute()`).
- [ ] **Commit-message format (every task):** conventional-commit subject, blank line, then the trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`. Use repeated `-m` flags.
- [ ] **Idempotency trap:** re-running the same `industry+location+as_of+time_window` returns the cached old job and `plan_job` short-circuits. To re-test the v8 prompt against the real LLM, change `--industry` or `--as-of`. Bumping `query_expansion.VERSION` v7 → v8 (Chunk 4) invalidates the combined Wave-0 cache automatically.

---

## File structure (decomposition locked here)

**New files:**
- `src/discovery/llm/schemas.py` — MODIFY: add `YouTubeQuerySpec`, add `JobPlan.youtube_queries`.
- `src/discovery/config/settings.py` — MODIFY: add `youtube_api_key`.
- `src/discovery/sources/youtube.py` — CREATE: the adapter (pure helpers + `YouTubeSource`). If it crosses ~500 lines at the end of Chunk 2, split pure helpers into `src/discovery/sources/youtube_helpers.py` (Task 2.6).
- `src/discovery/orchestrator/youtube.py` — CREATE: the orchestrator (compile + template + enqueue).
- `src/discovery/llm/stations/query_expansion.py` — MODIFY: generalize the carry-through helper.
- `src/discovery/llm/prompts/query_expansion.py` — MODIFY: Kind 4 section, VERSION v8, `build_user_message` line.
- `src/discovery/workers/__init__.py` — MODIFY: register `youtube`.
- `src/discovery/cli/run.py` — MODIFY: third fan-out branch.
- `.claude/skills/youtube-source/SKILL.md` — CREATE (owner-authorized).
- `docs/handoff.md` — MODIFY: append the "what shipped" section.

**New test files:**
- `tests/unit/llm/test_schemas.py` — MODIFY: `YouTubeQuerySpec`, `JobPlan.youtube_queries`.
- `tests/unit/sources/test_youtube.py` — CREATE.
- `tests/unit/test_orchestrator_youtube.py` — CREATE.
- `tests/unit/llm/stations/test_query_expansion.py` — MODIFY: generalized carry-through preserves both fields.
- `tests/unit/llm/test_prompts_query_expansion.py` — MODIFY: v8 + Kind 4 shape.
- `tests/unit/workers/test_registry.py` — MODIFY: youtube adapter present.
- `tests/unit/test_run_cli_parallel.py` — MODIFY: three-way fan-out.

---

## Chunk 1: Pure foundations — schema + config

Three small isolated additions with pure tests. No HTTP, no DB call out, no LLM. Each lands as its own atomic commit. Use @superpowers:test-driven-development for every task.

### Task 1.1: `YouTubeQuerySpec` schema

**Files:**
- Modify: `src/discovery/llm/schemas.py` — append after `HackerNewsKeywordSpec`, before `JobPlan`.
- Test: `tests/unit/llm/test_schemas.py` — add `TestYouTubeQuerySpec`.

**Spec reference:** §7.

- [ ] **Step 1: Write the failing tests.** Append to `tests/unit/llm/test_schemas.py` (ensure `YouTubeQuerySpec` is added to the existing `from discovery.llm.schemas import ...` line):

```python
class TestYouTubeQuerySpec:
    def test_minimal_valid(self) -> None:
        spec = YouTubeQuerySpec(
            query="why I quit commercial cleaning",
            intent="complaint",
            rationale="quit-the-industry pain monologue",
        )
        assert spec.query == "why I quit commercial cleaning"
        assert spec.intent == "complaint"

    def test_intent_must_be_complaint_or_discussion(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="x", intent="other", rationale="r")  # type: ignore[arg-type]

    def test_query_min_length(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="", intent="complaint", rationale="r")

    def test_query_max_length(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="x" * 121, intent="discussion", rationale="r")

    def test_rationale_min_length(self) -> None:
        with pytest.raises(ValidationError):
            YouTubeQuerySpec(query="x", intent="complaint", rationale="")

    def test_frozen_blocks_assignment(self) -> None:
        spec = YouTubeQuerySpec(query="x", intent="complaint", rationale="r")
        with pytest.raises(ValidationError):
            spec.query = "y"  # type: ignore[misc]
```

- [ ] **Step 2: Run; expect failure.**

```
wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run pytest tests/unit/llm/test_schemas.py::TestYouTubeQuerySpec -v"
```

Expected: `ImportError: cannot import name 'YouTubeQuerySpec'`.

- [ ] **Step 3: Implement.** In `src/discovery/llm/schemas.py`, append after `HackerNewsKeywordSpec` and before `JobPlan`:

```python
class YouTubeQuerySpec(BaseModel):
    """Wave 0 LLM YouTube search candidate. Python downstream normalizes,
    dedupes, applies the time-window publishedAfter floor, caps at
    MAX_YT_QUERIES, and runs the three-step fetch. See
    `docs/specs/2026-05-22-youtube-source-design.md` sections 7-10.
    """

    model_config = ConfigDict(frozen=True)

    query: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Full-text YouTube search phrase, emotion/pain-shaped and "
            "re-derived for THIS industry (e.g. 'why I quit commercial "
            "cleaning', 'Jobber vs Housecall Pro'). Used near-verbatim as "
            "the `q` parameter; YouTube is full-text relevance search, "
            "NOT token-AND, so no decomposition is applied."
        ),
    )
    intent: Literal["complaint", "discussion"] = Field(
        description=(
            "complaint -> the video itself is the pain (why-I-quit, "
            "horror stories, rant, worst-part, wish-I-knew). discussion "
            "-> the pain is in the comments and the video reveals "
            "tools/workflows (tutorials, tips, reviews, A-vs-B, "
            "day-in-the-life). Used for LLM generation balance and "
            "downstream tagging; does NOT route API params."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this YouTube candidate is worth running.",
    )
```

`Literal` is already imported at the top of `schemas.py` — no import change.

- [ ] **Step 4: Run tests + checks; expect pass.**

```
wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run pytest tests/unit/llm/test_schemas.py::TestYouTubeQuerySpec -v && uv run ruff check src tests && uv run mypy src/"
```

- [ ] **Step 5: Commit.**

```
git add src/discovery/llm/schemas.py tests/unit/llm/test_schemas.py
git commit -m "feat(llm): add YouTubeQuerySpec schema (Wave 0 YouTube candidate)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.2: Add `JobPlan.youtube_queries` typed field

**Files:**
- Modify: `src/discovery/llm/schemas.py` — add one field on `JobPlan`.
- Test: `tests/unit/llm/test_schemas.py` — add `TestJobPlanYoutubeQueries`.

**Spec reference:** §7 (permissive default load-bearing — a strict floor would let YouTube under-production raise `QueryExpansionError` and sink the Reddit grounded plan).

- [ ] **Step 1: Write the failing tests.** (`_make_reddit_queries` already exists in this file from the HN slice — reuse it. Add `YouTubeQuerySpec` to imports.)

```python
class TestJobPlanYoutubeQueries:
    def test_youtube_queries_defaults_to_empty_list(self) -> None:
        plan = JobPlan(reddit_queries=_make_reddit_queries())
        assert plan.youtube_queries == []

    def test_youtube_queries_accepts_list(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            youtube_queries=[
                YouTubeQuerySpec(query="day in the life plumber",
                                 intent="discussion", rationale="r"),
            ],
        )
        assert len(plan.youtube_queries) == 1
        assert plan.youtube_queries[0].intent == "discussion"

    def test_hn_and_youtube_coexist(self) -> None:
        plan = JobPlan(
            reddit_queries=_make_reddit_queries(),
            hn_queries=[HackerNewsKeywordSpec(keyword="CRM CLI", intent="launch", rationale="r")],
            youtube_queries=[YouTubeQuerySpec(query="x y", intent="complaint", rationale="r")],
        )
        assert len(plan.hn_queries) == 1
        assert len(plan.youtube_queries) == 1
```

- [ ] **Step 2: Run; expect failure** (`youtube_queries` defaults to nothing / attribute error).

```
wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run pytest tests/unit/llm/test_schemas.py::TestJobPlanYoutubeQueries -v"
```

- [ ] **Step 3: Implement.** Add the field on `JobPlan` after `hn_queries`:

```python
    youtube_queries: list[YouTubeQuerySpec] = Field(
        default_factory=list,
        description=(
            "Wave 0 YouTube search candidates. Permissive default (no "
            "min_length) is deliberate: a strict floor would let YouTube "
            "under-production raise QueryExpansionError and sink the "
            "Reddit grounded plan. Sparsity degrades to the no-LLM "
            "template in orchestrator/youtube.py."
        ),
    )
```

- [ ] **Step 4: Run tests + checks; expect pass.** (Also run the full `test_schemas.py` to confirm no regressions to the HN/Reddit cases.)
- [ ] **Step 5: Commit.**

```
git add src/discovery/llm/schemas.py tests/unit/llm/test_schemas.py
git commit -m "feat(llm): add JobPlan.youtube_queries (permissive default)" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 1.3: Add `youtube_api_key` setting

**Files:**
- Modify: `src/discovery/config/settings.py` — add one field by the other source keys.
- Test: `tests/unit/test_settings.py` — add a default-None assertion (follow the existing style in that file).

**Spec reference:** §11.

- [ ] **Step 1: Write the failing test.** Add (matching the file's existing settings-construction style):

```python
def test_youtube_api_key_defaults_to_none() -> None:
    from discovery.config.settings import Settings

    s = Settings(anthropic_api_key="x")  # type: ignore[call-arg]
    assert s.youtube_api_key is None
```

- [ ] **Step 2: Run; expect failure** (`AttributeError: youtube_api_key`).
- [ ] **Step 3: Implement.** In `settings.py`, add next to `google_api_key`:

```python
    youtube_api_key: SecretStr | None = None
```

- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit.**

```
git add src/discovery/config/settings.py tests/unit/test_settings.py
git commit -m "feat(config): add optional YOUTUBE_API_KEY setting" -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Chunk 2: The source adapter (`sources/youtube.py`)

Build the adapter bottom-up: pure URL builders, then record helpers, then the quota-aware request primitive + exceptions, then the three fetch helpers + `fetch`, then `aclose`. Each task is its own commit. End the chunk with a file-size check (Task 2.6).

Shared test scaffolding (put near the top of `tests/unit/sources/test_youtube.py`, mirroring `test_hackernews.py`):

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.base import RawRecord
from discovery.sources.youtube import (  # imports grow per task
    YouTubeSource,
)

_KEY = "test-key"


def _client_from_handler(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fast_limiter() -> AsyncLimiter:
    return AsyncLimiter(max_rate=1000, time_period=1)


async def _noop_sleep(_: float) -> None:
    return None
```

### Task 2.1: Pure URL builders

**Files:**
- Create: `src/discovery/sources/youtube.py`.
- Test: `tests/unit/sources/test_youtube.py` — `TestBuildUrls`.

**Spec reference:** §4 (endpoints/params), §10 (helpers).

- [ ] **Step 1: Write the failing tests.**

```python
from discovery.sources.youtube import (
    build_comments_url,
    build_search_url,
    build_videos_url,
)


def _search_query(query: str = "why I quit cleaning",
                  published_after: str | None = "2026-04-22T00:00:00Z") -> dict[str, Any]:
    return {
        "query": query,
        "order": "relevance",
        "type": "video",
        "part": "snippet",
        "published_after": published_after,
        "max_results": 50,
    }


class TestBuildUrls:
    def test_search_url_base_and_key(self) -> None:
        url = build_search_url(_search_query(), _KEY)
        assert url.startswith("https://www.googleapis.com/youtube/v3/search?")
        assert "key=test-key" in url

    def test_search_url_carries_params(self) -> None:
        url = build_search_url(_search_query(query="day in the life plumber"), _KEY)
        assert "q=day+in+the+life+plumber" in url
        assert "type=video" in url
        assert "order=relevance" in url
        assert "part=snippet" in url
        assert "maxResults=50" in url

    def test_search_url_includes_published_after_when_set(self) -> None:
        url = build_search_url(_search_query(published_after="2026-04-22T00:00:00Z"), _KEY)
        assert "publishedAfter=2026-04-22T00%3A00%3A00Z" in url

    def test_search_url_omits_published_after_when_none(self) -> None:
        url = build_search_url(_search_query(published_after=None), _KEY)
        assert "publishedAfter" not in url

    def test_videos_url_csv_ids_and_parts(self) -> None:
        url = build_videos_url(["vid1", "vid2", "vid3"], _KEY)
        assert url.startswith("https://www.googleapis.com/youtube/v3/videos?")
        assert "part=snippet%2Cstatistics" in url  # 'snippet,statistics' url-encoded
        assert "id=vid1%2Cvid2%2Cvid3" in url
        assert "key=test-key" in url

    def test_comments_url(self) -> None:
        url = build_comments_url("vid1", _KEY)
        assert url.startswith("https://www.googleapis.com/youtube/v3/commentThreads?")
        assert "videoId=vid1" in url
        assert "part=snippet" in url
        assert "order=relevance" in url
        assert "maxResults=100" in url
        assert "key=test-key" in url
```

- [ ] **Step 2: Run; expect ImportError.**
- [ ] **Step 3: Implement** the module header + the three builders:

```python
"""YouTube source adapter via the YouTube Data API v3.

See `.claude/skills/source-adapter/SKILL.md` for the umbrella contract,
`.claude/skills/youtube-source/SKILL.md` for the YouTube operational
rules, and `docs/specs/2026-05-22-youtube-source-design.md` for the
design. Quota (search.list = 100 units of 10,000/day) is the harshest
constraint; the adapter is built to be stingy with search and free with
the 1-unit enrichment + comment calls.

Three-step fetch: search.list -> videos.list (stats enrichment) ->
commentThreads.list (top COMMENT_TOP_K videos by view count). Quota-
aware retry: retry transient/rate-limit with backoff; hard-stop on
quotaExceeded (never retry into the wall).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import httpx
from aiolimiter import AsyncLimiter
from loguru import logger

from discovery.sources.base import BaseSource, RawRecord

_API_BASE = "https://www.googleapis.com/youtube/v3"


def build_search_url(query: dict[str, Any], api_key: str) -> str:
    """Build a search.list URL. `published_after` is omitted when None
    (time_window='all'). YouTube q is full-text (not token-AND)."""
    params: dict[str, str] = {
        "part": query["part"],
        "q": query["query"],
        "type": query["type"],
        "order": query["order"],
        "maxResults": str(query["max_results"]),
        "key": api_key,
    }
    published_after = query.get("published_after")
    if published_after is not None:
        params["publishedAfter"] = published_after
    return f"{_API_BASE}/search?{urlencode(params)}"


def build_videos_url(video_ids: list[str], api_key: str) -> str:
    """Build a videos.list URL for up to 50 ids (caller batches)."""
    params = {
        "part": "snippet,statistics",
        "id": ",".join(video_ids),
        "key": api_key,
    }
    return f"{_API_BASE}/videos?{urlencode(params)}"


def build_comments_url(video_id: str, api_key: str) -> str:
    """Build a commentThreads.list URL (top 100 relevance-ranked)."""
    params = {
        "part": "snippet",
        "videoId": video_id,
        "order": "relevance",
        "maxResults": "100",
        "key": api_key,
    }
    return f"{_API_BASE}/commentThreads?{urlencode(params)}"
```

- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit** (`feat(sources): YouTube Data API v3 URL builders`).

---

### Task 2.2: Record helpers + extractors

**Files:**
- Modify: `src/discovery/sources/youtube.py`.
- Test: `tests/unit/sources/test_youtube.py` — `TestRecordHelpers`.

**Spec reference:** §10.

- [ ] **Step 1: Write the failing tests.**

```python
from discovery.sources.youtube import (
    comment_to_raw_record,
    extract_video_ids,
    search_hit_to_raw_record,
    video_to_raw_record,
    viewcount_of,
)


class TestRecordHelpers:
    def test_extract_video_ids_skips_non_video_items(self) -> None:
        payload = {"items": [
            {"id": {"kind": "youtube#video", "videoId": "v1"}},
            {"id": {"kind": "youtube#channel", "channelId": "c1"}},  # no videoId
            {"id": {"kind": "youtube#video", "videoId": "v2"}},
        ]}
        assert extract_video_ids(payload) == ["v1", "v2"]

    def test_video_to_raw_record_verbatim(self) -> None:
        video = {"kind": "youtube#video", "id": "v1",
                 "snippet": {"title": "t"}, "statistics": {"viewCount": "1000"}}
        rec = video_to_raw_record(video)
        assert rec.source == "youtube"
        assert rec.external_id == "v1"
        assert rec.body == video  # verbatim

    def test_comment_to_raw_record_verbatim_carries_video_id(self) -> None:
        thread = {"kind": "youtube#commentThread", "id": "ct1",
                  "snippet": {"videoId": "v1", "topLevelComment": {"snippet": {"textDisplay": "x"}}}}
        rec = comment_to_raw_record(thread)
        assert rec.source == "youtube"
        assert rec.external_id == "ct1"
        assert rec.body["snippet"]["videoId"] == "v1"

    def test_search_hit_to_raw_record_uses_video_id(self) -> None:
        item = {"id": {"videoId": "v1"}, "snippet": {"title": "t"}}
        rec = search_hit_to_raw_record(item)
        assert rec.source == "youtube"
        assert rec.external_id == "v1"
        assert rec.body == item

    def test_viewcount_of_parses_string(self) -> None:
        assert viewcount_of({"statistics": {"viewCount": "1234"}}) == 1234

    def test_viewcount_of_missing_defaults_zero(self) -> None:
        assert viewcount_of({"statistics": {}}) == 0
        assert viewcount_of({}) == 0
```

- [ ] **Step 2: Run; expect ImportError.**
- [ ] **Step 3: Implement.**

```python
def extract_video_ids(search_payload: dict[str, Any]) -> list[str]:
    """Pull videoIds from a search.list payload, skipping non-video items
    (defensive even with type=video)."""
    out: list[str] = []
    for item in search_payload.get("items", []):
        vid = item.get("id", {}).get("videoId")
        if vid:
            out.append(str(vid))
    return out


def video_to_raw_record(video: dict[str, Any]) -> RawRecord:
    """videos.list resource -> RawRecord. Verbatim (kind=youtube#video)."""
    return RawRecord(source="youtube", external_id=str(video["id"]), body=video)


def comment_to_raw_record(thread: dict[str, Any]) -> RawRecord:
    """commentThreads.list resource -> RawRecord. Verbatim; body carries
    snippet.videoId so the video link survives (kind=youtube#commentThread)."""
    return RawRecord(source="youtube", external_id=str(thread["id"]), body=thread)


def search_hit_to_raw_record(item: dict[str, Any]) -> RawRecord:
    """Fallback only (enrichment quota-stop): store the search item
    verbatim (kind=youtube#searchResult)."""
    return RawRecord(source="youtube", external_id=str(item["id"]["videoId"]), body=item)


def viewcount_of(video: dict[str, Any]) -> int:
    """Parse statistics.viewCount (a string) to int; 0 when absent
    (live/upcoming videos can lack it -> they sort last for comments)."""
    raw = video.get("statistics", {}).get("viewCount")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
```

- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit** (`feat(sources): YouTube record helpers + extractors`).

---

### Task 2.3: Exceptions + quota-aware `_get_json` primitive

**Files:**
- Modify: `src/discovery/sources/youtube.py` — add exceptions, the `YouTubeSource` class shell (constructor + `_get_json` + `aclose`), constants.
- Test: `tests/unit/sources/test_youtube.py` — `TestGetJson`, `TestAclose`.

**Spec reference:** §10 (quota-aware retry, classification inside the loop, hand-rolled injectable sleep).

- [ ] **Step 1: Write the failing tests.** Cover: 200 returns parsed JSON; a `quotaExceeded` 403 raises `YouTubeQuotaExceeded` on the FIRST call (no retry); a `commentsDisabled` 403 raises `CommentsDisabled` first-call; a 500 is retried then succeeds (count the calls, inject `_noop_sleep`); a `rateLimitExceeded` 403 is retried; a 5xx that never recovers raises after `max_retries+1` attempts.

```python
from discovery.sources.youtube import CommentsDisabled, YouTubeQuotaExceeded


def _error_response(status: int, reason: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"errors": [{"reason": reason}], "code": status}})


def _src(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> YouTubeSource:
    return YouTubeSource(
        api_key=_KEY,
        client=_client_from_handler(handler),
        limiter=_fast_limiter(),
        sleep=_noop_sleep,
        **kw,
    )


class TestGetJson:
    async def test_returns_parsed_json_on_200(self) -> None:
        src = _src(lambda _: httpx.Response(200, json={"ok": True}))
        try:
            assert await src._get_json("https://x/") == {"ok": True}
        finally:
            await src.aclose()

    async def test_quota_exceeded_raises_first_call_no_retry(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return _error_response(403, "quotaExceeded")

        src = _src(handler)
        try:
            with pytest.raises(YouTubeQuotaExceeded):
                await src._get_json("https://x/")
            assert calls["n"] == 1
        finally:
            await src.aclose()

    async def test_comments_disabled_raises_first_call(self) -> None:
        src = _src(lambda _: _error_response(403, "commentsDisabled"))
        try:
            with pytest.raises(CommentsDisabled):
                await src._get_json("https://x/")
        finally:
            await src.aclose()

    async def test_transient_500_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, json={"ok": True})

        src = _src(handler)
        try:
            assert await src._get_json("https://x/") == {"ok": True}
            assert calls["n"] == 2  # retried once
        finally:
            await src.aclose()

    async def test_rate_limit_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return _error_response(403, "rateLimitExceeded")
            return httpx.Response(200, json={"ok": True})

        src = _src(handler)
        try:
            assert await src._get_json("https://x/") == {"ok": True}
            assert calls["n"] == 2
        finally:
            await src.aclose()

    async def test_persistent_5xx_raises_after_budget(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503)

        src = _src(handler, max_retries=2)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await src._get_json("https://x/")
            assert calls["n"] == 3  # 1 + 2 retries
        finally:
            await src.aclose()


class TestAclose:
    async def test_aclose_closes_owned_client(self) -> None:
        src = YouTubeSource(api_key=_KEY, limiter=_fast_limiter())
        assert not src._client.is_closed
        await src.aclose()
        assert src._client.is_closed

    async def test_aclose_does_not_close_injected_client(self) -> None:
        injected = httpx.AsyncClient()
        try:
            src = YouTubeSource(api_key=_KEY, client=injected, limiter=_fast_limiter())
            await src.aclose()
            assert not injected.is_closed
        finally:
            await injected.aclose()
```

- [ ] **Step 2: Run; expect ImportError / failures.**
- [ ] **Step 3: Implement** the exceptions, constants, class shell. `_get_json` mirrors `RedditSource._fetch_with_retries` but classifies 403 reasons. Keep `_get_json` and its helpers each <=60 lines (split the reason-classification into `_classify_403`).

```python
# --- constants ----------------------------------------------------------
COMMENT_TOP_K = 50          # videos to harvest comments from, by view count
VIDEOS_BATCH = 50           # max ids per videos.list call
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


# --- exceptions ---------------------------------------------------------
class YouTubeQuotaExceeded(Exception):
    """Daily quota gone (403 quotaExceeded/dailyLimitExceeded). Terminal:
    never retried; the caller stops cleanly and keeps partial results."""


class YouTubeRateLimited(Exception):
    """Too-fast (403/429 rateLimitExceeded). Transient: retried."""


class CommentsDisabled(Exception):
    """commentThreads.list 403 commentsDisabled. Per-video skip."""


def _reason_of(response: httpx.Response) -> str | None:
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return None
    return errors[0].get("reason") if errors else None
```

`YouTubeSource.__init__` mirrors Reddit/HN with `api_key` + injectable `sleep` + `max_retries`:

```python
class YouTubeSource(BaseSource):
    name = "youtube"
    rate_limit = (5, 1)  # 5 req/s polite; quota (not rate) is the real ceiling

    def __init__(
        self,
        *,
        api_key: str | None,
        client: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        self._owned_client = client is None
        self._limiter = limiter if limiter is not None else AsyncLimiter(max_rate=5, time_period=1)
        self._sleep = sleep
        self._max_retries = max_retries

    async def _get_json(self, url: str) -> dict[str, Any]:
        """GET with quota-aware retry. Classifies 403 reasons BEFORE the
        retry decision: quota/commentsDisabled raise immediately; rate-
        limit + 5xx + network errors retry with backoff (5s,10s,20s cap
        300s). Mirrors RedditSource._fetch_with_retries."""
        for attempt in range(self._max_retries + 1):
            try:
                async with self._limiter:
                    response = await self._client.get(url)
            except httpx.HTTPError:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_seconds(attempt))
                    continue
                raise
            retry = self._classify(response, attempt)
            if retry is None:
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            await self._sleep(retry)
        raise RuntimeError("unreachable: retry loop exited")  # pragma: no cover

    def _classify(self, response: httpx.Response, attempt: int) -> float | None:
        """Return a backoff delay to retry, or None to accept/raise. Raises
        the terminal exceptions directly."""
        if response.status_code == 403:
            reason = _reason_of(response)
            if reason in _QUOTA_REASONS:
                raise YouTubeQuotaExceeded(reason or "quotaExceeded")
            if reason == "commentsDisabled":
                raise CommentsDisabled("commentsDisabled")
            if reason in _RATE_REASONS:
                if attempt < self._max_retries:
                    return self._backoff_seconds(attempt)
                raise YouTubeRateLimited(reason or "rateLimitExceeded")
            return None  # other 403 -> non-retryable, raise_for_status handles it
        if 500 <= response.status_code < 600 and attempt < self._max_retries:
            return self._backoff_seconds(attempt)
        return None

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        return min(5.0 * (2.0**attempt), 300.0)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()
```

> **Note on the `test_persistent_5xx` case:** at the final attempt `_classify` returns None for a 5xx, so `response.raise_for_status()` raises `HTTPStatusError` — matching the test. For `rateLimitExceeded` at the final attempt, `_classify` raises `YouTubeRateLimited`. Keep both behaviors.

- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit** (`feat(sources): YouTube quota-aware _get_json + exceptions`).

---

### Task 2.4: The three fetch helpers + `fetch`

**Files:**
- Modify: `src/discovery/sources/youtube.py` — add `_search_all`, `_enrich_videos`, `_harvest_comments`, `fetch`.
- Test: `tests/unit/sources/test_youtube.py` — `TestFetch`.

**Spec reference:** §10 (the `fetch(params): 0..4` flow).

- [ ] **Step 1: Write the failing tests.** A routing handler keyed on the URL path lets one MockTransport serve all three endpoints. Cover: no-key no-op (zero HTTP calls); happy path (videos + comments records); search partial success; quota-stop on second search query (third NOT attempted); enrichment quota-stop → search-hit fallback records + no comments; commentsDisabled skips one video; all-searches-fail raises.

```python
def _routing_handler(
    *,
    search_pages: dict[str, list[str]],
    stats: dict[str, str] | None = None,
    disabled_videos: set[str] | None = None,
    quota_on: set[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """search_pages: q-substring -> list of videoIds. stats: vid -> viewCount.
    quota_on: substrings that should 403 quotaExceeded. disabled_videos:
    videoIds whose comment call 403s commentsDisabled."""
    stats = stats or {}
    disabled_videos = disabled_videos or set()
    quota_on = quota_on or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/search?" in url:
            q = _single_param(url, "q")  # decoded; robust vs `+`-encoding
            if any(tok in q for tok in quota_on):
                return _error_response(403, "quotaExceeded")
            for needle, ids in search_pages.items():
                if needle in q:
                    return httpx.Response(200, json={"items": [
                        {"kind": "youtube#searchResult",
                         "id": {"kind": "youtube#video", "videoId": v},
                         "snippet": {"title": v}} for v in ids]})
            return httpx.Response(200, json={"items": []})
        if "/videos?" in url:
            ids = _ids_from_query(url, "id")
            return httpx.Response(200, json={"items": [
                {"kind": "youtube#video", "id": v, "snippet": {"title": v},
                 "statistics": {"viewCount": stats.get(v, "0")}} for v in ids]})
        if "/commentThreads?" in url:
            vid = _single_param(url, "videoId")
            if vid in disabled_videos:
                return _error_response(403, "commentsDisabled")
            return httpx.Response(200, json={"items": [
                {"kind": "youtube#commentThread", "id": f"{vid}-c1",
                 "snippet": {"videoId": vid}}]})
        return httpx.Response(404)

    return handler
```

The two URL-parse test helpers (decoded matching is robust against `urlencode`'s `+`-for-space and avoids accidental substring hits elsewhere in the URL):

```python
from urllib.parse import parse_qs, urlparse


def _single_param(url: str, name: str) -> str:
    return parse_qs(urlparse(url).query).get(name, [""])[0]


def _ids_from_query(url: str, name: str) -> list[str]:
    raw = _single_param(url, name)
    return raw.split(",") if raw else []
```

Then the cases:

```python
class TestFetch:
    async def test_no_key_is_noop(self) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={"items": []})

        src = YouTubeSource(api_key=None, client=_client_from_handler(handler),
                            limiter=_fast_limiter(), sleep=_noop_sleep)
        try:
            assert await src.fetch({"queries": [_search_query()]}) == []
            assert calls["n"] == 0
        finally:
            await src.aclose()

    async def test_happy_path_returns_video_and_comment_records(self) -> None:
        handler = _routing_handler(search_pages={"why": ["v1", "v2"]},
                                   stats={"v1": "500", "v2": "10"})
        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query(query="why I quit cleaning")]})
            kinds = {r.body.get("kind") for r in records}
            assert "youtube#video" in kinds
            assert "youtube#commentThread" in kinds
            assert {r.external_id for r in records if r.body.get("kind") == "youtube#video"} == {"v1", "v2"}
            assert all(r.source == "youtube" for r in records)
        finally:
            await src.aclose()

    async def test_quota_stop_on_second_search_skips_third(self) -> None:
        """query 2 hits the wall -> query 3 is NOT attempted; query-1's
        video still flows through enrichment + comments."""
        searched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                q = _single_param(url, "q")
                searched.append(q)
                if "wall" in q:
                    return _error_response(403, "quotaExceeded")
                return httpx.Response(200, json={"items": [
                    {"id": {"kind": "youtube#video", "videoId": "v1"}, "snippet": {}}]})
            if "/videos?" in url:
                ids = _ids_from_query(url, "id")
                return httpx.Response(200, json={"items": [
                    {"kind": "youtube#video", "id": v, "snippet": {},
                     "statistics": {"viewCount": "1"}} for v in ids]})
            if "/commentThreads?" in url:
                vid = _single_param(url, "videoId")
                return httpx.Response(200, json={"items": [
                    {"kind": "youtube#commentThread", "id": f"{vid}-c", "snippet": {"videoId": vid}}]})
            return httpx.Response(404)

        src = _src(handler)
        try:
            records = await src.fetch({"queries": [
                _search_query(query="first ok"),
                _search_query(query="wall hit"),
                _search_query(query="third never"),
            ]})
            assert searched == ["first ok", "wall hit"]  # query 3 never fired
            assert any(r.body.get("kind") == "youtube#video" for r in records)
        finally:
            await src.aclose()

    async def test_enrichment_quota_stop_falls_back_to_search_hits(self) -> None:
        """videos.list 403 quotaExceeded -> un-enriched ids stored as
        kind=youtube#searchResult; NO comment records (no stats to rank,
        quota gone)."""
        commented = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                return httpx.Response(200, json={"items": [
                    {"kind": "youtube#searchResult",
                     "id": {"kind": "youtube#video", "videoId": "v1"},
                     "snippet": {"title": "t"}}]})
            if "/videos?" in url:
                return _error_response(403, "quotaExceeded")
            if "/commentThreads?" in url:
                commented["n"] += 1
                return httpx.Response(200, json={"items": []})
            return httpx.Response(404)

        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query()]})
            assert {r.body.get("kind") for r in records} == {"youtube#searchResult"}
            assert records[0].external_id == "v1"
            assert commented["n"] == 0  # comment harvest skipped after enrichment quota-stop
        finally:
            await src.aclose()

    async def test_comments_disabled_skips_one_video(self) -> None:
        """commentsDisabled on v1 -> v1 skipped, v2 harvested; BOTH videos
        still stored."""
        handler = _routing_handler(
            search_pages={"why": ["v1", "v2"]},
            stats={"v1": "100", "v2": "50"},
            disabled_videos={"v1"},
        )
        src = _src(handler)
        try:
            records = await src.fetch({"queries": [_search_query(query="why")]})
            comment_vids = {
                r.body["snippet"]["videoId"]
                for r in records if r.body.get("kind") == "youtube#commentThread"
            }
            video_ids = {
                r.external_id for r in records if r.body.get("kind") == "youtube#video"
            }
            assert comment_vids == {"v2"}
            assert video_ids == {"v1", "v2"}
        finally:
            await src.aclose()

    async def test_top_k_limits_comment_videos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the COMMENT_TOP_K highest-view videos get a comment call."""
        import discovery.sources.youtube as yt

        monkeypatch.setattr(yt, "COMMENT_TOP_K", 1)
        commented: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/search?" in url:
                return httpx.Response(200, json={"items": [
                    {"id": {"kind": "youtube#video", "videoId": v}, "snippet": {}}
                    for v in ["low", "high"]]})
            if "/videos?" in url:
                ids = _ids_from_query(url, "id")
                views = {"low": "5", "high": "9999"}
                return httpx.Response(200, json={"items": [
                    {"kind": "youtube#video", "id": v, "snippet": {},
                     "statistics": {"viewCount": views[v]}} for v in ids]})
            if "/commentThreads?" in url:
                commented.append(_single_param(url, "videoId"))
                return httpx.Response(200, json={"items": []})
            return httpx.Response(404)

        src = _src(handler)
        try:
            await src.fetch({"queries": [_search_query()]})
            assert commented == ["high"]  # top-1 by viewcount only
        finally:
            await src.aclose()

    async def test_all_searches_fail_raises(self) -> None:
        src = _src(lambda _: httpx.Response(500), max_retries=0)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await src.fetch({"queries": [_search_query(), _search_query(query="other")]})
        finally:
            await src.aclose()
```

`_harvest_comments` reads the module-global `COMMENT_TOP_K` at call time, so the `monkeypatch.setattr(yt, "COMMENT_TOP_K", 1)` above takes effect.

- [ ] **Step 2: Run; expect failures.**
- [ ] **Step 3: Implement** `fetch` as a thin orchestrator over the three helpers (each <=60 lines):

```python
    async def fetch(self, params: dict[str, Any]) -> list[RawRecord]:
        if self._api_key is None:
            logger.warning("youtube: no API key configured; skipping (0 records)")
            return []
        ids, items_by_id = await self._search_all(params.get("queries", []))
        if not ids:
            return []
        video_records, enriched = await self._enrich_videos(ids, items_by_id)
        comment_records = await self._harvest_comments(enriched)
        return video_records + comment_records

    async def _search_all(
        self, queries: list[dict[str, Any]]
    ) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """Run search.list per query. Returns (ordered unique videoIds,
        items_by_id). Stops on quota; partial success otherwise. Raises
        the first error only when nothing was gathered and all errored."""
        ordered: list[str] = []
        items_by_id: dict[str, dict[str, Any]] = {}
        errors: list[Exception] = []
        assert self._api_key is not None
        for q in queries:
            try:
                payload = await self._get_json(build_search_url(q, self._api_key))
            except YouTubeQuotaExceeded:
                logger.warning("youtube: quota exhausted during search; stopping early")
                break
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("youtube search failed", query=q, error=str(exc))
                errors.append(exc)
                continue
            for item in payload.get("items", []):
                vid = item.get("id", {}).get("videoId")
                if vid and vid not in items_by_id:
                    items_by_id[vid] = item
                    ordered.append(str(vid))
            self._log_call("search", build_search_url(q, self._api_key),
                           count=len(payload.get("items", [])))
        if not ordered and errors:
            raise errors[0]
        return ordered, items_by_id

    async def _enrich_videos(
        self, ids: list[str], items_by_id: dict[str, dict[str, Any]]
    ) -> tuple[list[RawRecord], list[dict[str, Any]]]:
        """Batch ids into videos.list. On quota-stop, emit search-hit
        fallback records for the un-enriched ids."""
        records: list[RawRecord] = []
        enriched: list[dict[str, Any]] = []
        assert self._api_key is not None
        done: set[str] = set()
        for i in range(0, len(ids), VIDEOS_BATCH):
            batch = ids[i : i + VIDEOS_BATCH]
            try:
                payload = await self._get_json(build_videos_url(batch, self._api_key))
            except YouTubeQuotaExceeded:
                logger.warning("youtube: quota exhausted during enrichment; storing search hits")
                for vid in ids:
                    if vid not in done:
                        records.append(search_hit_to_raw_record(items_by_id[vid]))
                return records, enriched
            for video in payload.get("items", []):
                enriched.append(video)
                records.append(video_to_raw_record(video))
                done.add(str(video.get("id")))
        return records, enriched

    async def _harvest_comments(self, enriched: list[dict[str, Any]]) -> list[RawRecord]:
        """commentThreads.list for the top COMMENT_TOP_K videos by view
        count. Skips commentsDisabled videos; stops on quota."""
        records: list[RawRecord] = []
        assert self._api_key is not None
        ranked = sorted(enriched, key=viewcount_of, reverse=True)[:COMMENT_TOP_K]
        for video in ranked:
            vid = str(video.get("id"))
            try:
                payload = await self._get_json(build_comments_url(vid, self._api_key))
            except CommentsDisabled:
                logger.debug("youtube: comments disabled, skipping", video_id=vid)
                continue
            except YouTubeQuotaExceeded:
                logger.warning("youtube: quota exhausted during comment harvest; stopping")
                break
            for thread in payload.get("items", []):
                records.append(comment_to_raw_record(thread))
        return records

    def _log_call(self, kind: str, url: str, *, count: int) -> None:
        """Per-call diagnostic; key redacted (never log the key)."""
        logger.info("youtube call", kind=kind, url=_redact_key(url), count=count)
```

Add a module-level `_redact_key(url)` that replaces the `key=` value with `REDACTED` (regex or `urlencode` round-trip). Add `import re` if used.

- [ ] **Step 4: Run tests + checks; expect pass.** Confirm no function exceeds 60 lines (`fetch` is ~7 lines; each helper <=60).
- [ ] **Step 5: Commit** (`feat(sources): YouTube three-step fetch (search/enrich/comments)`).

---

### Task 2.5: Logging assertion + key-redaction test

**Files:**
- Test: `tests/unit/sources/test_youtube.py` — `TestLogging`.

- [ ] **Step 1: Write the test** — a loguru sink (as in `test_hackernews.py`) asserts a per-call log line carries `kind`, redacted `url`, `count`, AND that the raw `test-key` value never appears in any captured log line.
- [ ] **Step 2-4:** Run; if `_redact_key` already implemented in 2.4 this passes immediately — if not, implement it. Checks green.
- [ ] **Step 5: Commit** (`test(sources): YouTube per-call logging + key redaction`).

---

### Task 2.6: File-size check + optional split

**Spec reference:** §10 file-size note.

- [ ] **Step 1:** Count lines: `wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && wc -l src/discovery/sources/youtube.py"`.
- [ ] **Step 2:** If <=500 lines, do nothing (note it in the commit message of the next chunk). If >500, split the pure helpers (`build_*_url`, `*_to_raw_record`, `extract_video_ids`, `viewcount_of`, `_reason_of`, `_redact_key`, the exceptions) into `src/discovery/sources/youtube_helpers.py`, re-import them in `youtube.py`, update test imports. Run full checks. Commit (`refactor(sources): split YouTube pure helpers into youtube_helpers.py`).

---

## Chunk 3: The orchestrator (`orchestrator/youtube.py`)

Mirror `orchestrator/hackernews.py`. Build: time-window helper → compile → template → enqueue. Tests go in `tests/unit/test_orchestrator_youtube.py` (mirror `test_orchestrator_hackernews.py`, including the in-memory `session` fixture and `_make_job` / `_make_reddit_queries` helpers — copy them into this file).

### Task 3.1: `_time_window_rfc3339`

**Files:**
- Create: `src/discovery/orchestrator/youtube.py` (module header + helper).
- Test: `tests/unit/test_orchestrator_youtube.py` — `TestTimeWindowRfc3339`.

**Spec reference:** §9 (offset table identical to HN; output is an RFC 3339 `...Z` string).

- [ ] **Step 1: Write the failing tests.**

```python
from datetime import date
from discovery.orchestrator.youtube import _time_window_rfc3339


class TestTimeWindowRfc3339:
    def test_day_window(self) -> None:
        assert _time_window_rfc3339("day", date(2026, 5, 22)) == "2026-05-21T00:00:00Z"

    def test_hour_window(self) -> None:
        assert _time_window_rfc3339("hour", date(2026, 5, 22)) == "2026-05-21T23:00:00Z"

    def test_week_window(self) -> None:
        assert _time_window_rfc3339("week", date(2026, 5, 22)) == "2026-05-15T00:00:00Z"

    def test_month_window_30_days(self) -> None:
        assert _time_window_rfc3339("month", date(2026, 5, 22)) == "2026-04-22T00:00:00Z"

    def test_year_window_365_days(self) -> None:
        assert _time_window_rfc3339("year", date(2026, 5, 22)) == "2025-05-22T00:00:00Z"

    def test_all_returns_none(self) -> None:
        assert _time_window_rfc3339("all", date(2026, 5, 22)) is None

    def test_unknown_window_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown time window"):
            _time_window_rfc3339("decade", date(2026, 5, 22))
```

- [ ] **Step 2: Run; expect ImportError.**
- [ ] **Step 3: Implement** the module header + helper (offset table copied from HN):

```python
"""Wave 1 orchestration for YouTube.

Bridges Wave 0 (`JobPlan.youtube_queries`) and the YouTube adapter's
fetch-params dict. Mechanical rules live here in tested Python: the
RFC 3339 publishedAfter floor from JobSpec.time_window, dedup, the
MAX_YT_QUERIES cap. No token decomposition (YouTube is full-text, not
token-AND). Falls back to a deterministic pain-shaped template when
job_plan is null/invalid. See
`docs/specs/2026-05-22-youtube-source-design.md` sections 9-10.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import Job, Task
from discovery.hashing import hash_params
from discovery.jobs import JobSpec
from discovery.llm.schemas import JobPlan, YouTubeQuerySpec

MAX_YT_QUERIES: int = 10

_TIME_WINDOW_SECONDS: dict[str, int] = {
    "hour": 3_600,
    "day": 86_400,
    "week": 604_800,
    "month": 30 * 86_400,
    "year": 365 * 86_400,
}


def _time_window_rfc3339(time_window: str, as_of: date) -> str | None:
    """Unix-window floor as an RFC 3339 'YYYY-MM-DDTHH:MM:SSZ' string,
    anchored at `as_of` midnight UTC. `all` -> None (omit publishedAfter)."""
    if time_window == "all":
        return None
    if time_window not in _TIME_WINDOW_SECONDS:
        raise ValueError(f"unknown time window: {time_window!r}")
    anchor = datetime.combine(as_of, time.min, tzinfo=UTC)
    floor = anchor - timedelta(seconds=_TIME_WINDOW_SECONDS[time_window])
    return floor.strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit** (`feat(orchestrator): YouTube time-window RFC 3339 floor`).

---

### Task 3.2: `_compile_yt_queries` + `_build_fetch_params`

**Files:**
- Modify: `src/discovery/orchestrator/youtube.py`.
- Test: `tests/unit/test_orchestrator_youtube.py` — `TestCompileYtQueries`.

**Spec reference:** §9.

- [ ] **Step 1: Write the failing tests.** Cover: normalize/strip; dedup on the lowercased normalized query; cap at `MAX_YT_QUERIES=10` preserving LLM order; `published_after` present when window != all and None when all; constant fields (`order=relevance`, `type=video`, `part=snippet`, `max_results=50`).

```python
def _yt(query: str, intent: str = "complaint") -> YouTubeQuerySpec:
    return YouTubeQuerySpec(query=query, intent=intent, rationale="r")  # type: ignore[arg-type]


def _spec(industry: str = "cleaning", time_window: str = "month") -> JobSpec:
    return JobSpec(industry=industry, as_of=date(2026, 5, 22),
                   time_window=time_window)  # type: ignore[arg-type]


class TestCompileYtQueries:
    def test_normalizes_and_strips(self) -> None:
        out = _compile_yt_queries([_yt("  why  I quit  cleaning ")], _spec())
        assert out[0]["query"] == "why I quit cleaning"

    def test_dedups_case_insensitively(self) -> None:
        out = _compile_yt_queries([_yt("Why I Quit"), _yt("why i quit")], _spec())
        assert len(out) == 1

    def test_caps_at_max_preserving_order(self) -> None:
        out = _compile_yt_queries([_yt(f"q{i} x") for i in range(20)], _spec())
        assert len(out) == MAX_YT_QUERIES == 10
        assert out[0]["query"] == "q0 x"
        assert out[9]["query"] == "q9 x"

    def test_published_after_present_for_month(self) -> None:
        out = _compile_yt_queries([_yt("x")], _spec(time_window="month"))
        assert out[0]["published_after"] == "2026-04-22T00:00:00Z"

    def test_published_after_none_for_all(self) -> None:
        out = _compile_yt_queries([_yt("x")], _spec(time_window="all"))
        assert out[0]["published_after"] is None

    def test_constant_fields(self) -> None:
        out = _compile_yt_queries([_yt("x")], _spec())
        assert out[0]["order"] == "relevance"
        assert out[0]["type"] == "video"
        assert out[0]["part"] == "snippet"
        assert out[0]["max_results"] == 50
```

- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement.**

```python
def _normalize_query(query: str) -> str:
    return " ".join(query.split())


def _build_fetch_params(query: str, published_after: str | None) -> dict[str, Any]:
    return {
        "query": query,
        "order": "relevance",
        "type": "video",
        "part": "snippet",
        "published_after": published_after,
        "max_results": 50,
    }


def _compile_yt_queries(
    specs: Iterable[YouTubeQuerySpec], job_spec: JobSpec
) -> list[dict[str, Any]]:
    """Normalize -> dedup (case-insensitive) -> publishedAfter -> cap.
    Preserves the LLM's emission order (a ranking signal). No token
    decomposition (YouTube is full-text relevance, not token-AND)."""
    published_after = _time_window_rfc3339(job_spec.time_window, job_spec.as_of)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for spec in specs:
        query = _normalize_query(spec.query)
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_build_fetch_params(query, published_after))
        if len(out) >= MAX_YT_QUERIES:
            break
    return out
```

- [ ] **Step 4: Run; checks green.**
- [ ] **Step 5: Commit** (`feat(orchestrator): YouTube compile pipeline (dedup, cap, publishedAfter)`).

---

### Task 3.3: `youtube_queries_for_spec` template fallback

**Files:**
- Modify: `src/discovery/orchestrator/youtube.py`.
- Test: `tests/unit/test_orchestrator_youtube.py` — `TestYoutubeQueriesForSpec`.

**Spec reference:** §9.

- [ ] **Step 1: Write the failing tests.** Template emits 5 pain-shaped candidates from the industry literal; all compile; each carries the time-window `published_after`; deterministic.

```python
class TestYoutubeQueriesForSpec:
    def test_returns_compiled_queries(self) -> None:
        out = youtube_queries_for_spec(_spec(industry="cleaning"))
        assert 1 <= len(out) <= MAX_YT_QUERIES
        assert any("quit" in q["query"] for q in out)

    def test_each_carries_published_after(self) -> None:
        out = youtube_queries_for_spec(_spec(industry="cleaning", time_window="year"))
        assert all(q["published_after"] == "2025-05-22T00:00:00Z" for q in out)

    def test_deterministic(self) -> None:
        s = _spec(industry="cleaning")
        assert youtube_queries_for_spec(s) == youtube_queries_for_spec(s)
```

- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement** (per spec §9):

```python
def youtube_queries_for_spec(spec: JobSpec) -> list[dict[str, Any]]:
    """Deterministic no-LLM fallback -- pain-shaped phrases off the
    industry literal. Used when job_plan is null/invalid. Same compile
    path as the LLM output. Mirrors hn_keyword_candidates_for_spec."""
    industry = spec.industry
    candidates = [
        YouTubeQuerySpec(query=f"why I quit {industry}", intent="complaint",
                         rationale="(template) quit-the-industry pain monologue"),
        YouTubeQuerySpec(query=f"{industry} horror stories", intent="complaint",
                         rationale="(template) compiled pain across many people"),
        YouTubeQuerySpec(query=f"things nobody tells you about {industry}",
                         intent="complaint", rationale="(template) retrospective pain"),
        YouTubeQuerySpec(query=f"{industry} tutorial", intent="discussion",
                         rationale="(template) comments hold 'this breaks for me' pain"),
        YouTubeQuerySpec(query=f"day in the life {industry}", intent="discussion",
                         rationale="(template) visible workflow friction"),
    ]
    return _compile_yt_queries(candidates, spec)
```

- [ ] **Step 4-5:** Run; checks green. Commit (`feat(orchestrator): YouTube no-LLM pain-shaped template fallback`).

---

### Task 3.4: `_queries_from_job_plan` + `enqueue_youtube_task_for_job`

**Files:**
- Modify: `src/discovery/orchestrator/youtube.py`.
- Test: `tests/unit/test_orchestrator_youtube.py` — `TestQueriesFromJobPlan`, `TestEnqueueYoutubeTaskForJob` (mirror the HN versions exactly: null → None/template, empty → [] graceful, present → compiled, invalid → None, idempotent on content_hash).

**Spec reference:** §9.

- [ ] **Step 1: Write the failing tests** (copy the HN structure; swap `hackernews`→`youtube`, `hn_queries`→`youtube_queries`, assert `task.source == "youtube"`). Include the graceful-empty case (valid plan, empty `youtube_queries` → `[]`, still enqueues a no-op task).
- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement** `_queries_from_job_plan` and `enqueue_youtube_task_for_job` mirroring HN (note the `is None` vs `or` subtlety — empty list is graceful sparsity, only null/invalid triggers the template):

```python
def _queries_from_job_plan(job: Job) -> list[dict[str, Any]] | None:
    if job.job_plan is None:
        return None
    try:
        plan = JobPlan.model_validate(job.job_plan)
    except Exception as e:
        logger.warning("job {} job_plan fails validation ({}); YouTube template fallback.",
                       job.id, e)
        return None
    spec = JobSpec.model_validate(job.spec)
    return _compile_yt_queries(plan.youtube_queries, spec)


async def enqueue_youtube_task_for_job(session: AsyncSession, job: Job) -> Task:
    spec = JobSpec.model_validate(job.spec)
    queries = _queries_from_job_plan(job)
    if queries is None:  # null/invalid plan -> template; empty list is graceful sparsity
        queries = youtube_queries_for_spec(spec)
    params: dict[str, Any] = {"queries": queries}
    content_hash = hash_params({"source": "youtube", "action": "fetch", "params": params})

    existing = await session.exec(
        select(Task).where(Task.job_id == job.id, Task.content_hash == content_hash)
    )
    task = existing.first()
    if task is not None:
        return task
    task = Task(job_id=job.id, wave=1, source="youtube", action="fetch",
               params=params, content_hash=content_hash)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task
```

- [ ] **Step 4-5:** Run; checks green. Commit (`feat(orchestrator): enqueue_youtube_task_for_job (idempotent, template fallback)`).

---

## Chunk 4: Wave-0 wiring — generalized carry-through + v8 prompt

### Task 4.1: Generalize the carry-through helper

**Files:**
- Modify: `src/discovery/llm/stations/query_expansion.py` — replace `_attach_hn_queries` with `_attach_extra_source_queries`; update the two wiring lines in `run_query_expansion`.
- Test: `tests/unit/llm/stations/test_query_expansion.py` — update/extend the carry-through test to assert BOTH `hn_queries` and `youtube_queries` survive a simulated locked-tail rebuild.

**Spec reference:** §6.

- [ ] **Step 1: Write the failing test.** Add (or adapt the existing `_attach_hn_queries` test):

```python
def test_attach_extra_source_queries_preserves_both_fields() -> None:
    from discovery.llm.schemas import HackerNewsKeywordSpec, JobPlan, YouTubeQuerySpec
    from discovery.llm.stations.query_expansion import _attach_extra_source_queries

    # Simulate a post-tail plan: model_construct with ONLY reddit fields
    # (the locked tail drops non-reddit fields).
    post_tail = JobPlan.model_construct(reddit_queries=[], reddit_subreddits=["startups"])
    hn = [HackerNewsKeywordSpec(keyword="CRM CLI", intent="launch", rationale="r")]
    yt = [YouTubeQuerySpec(query="why I quit x", intent="complaint", rationale="r")]

    out = _attach_extra_source_queries(post_tail, hn_queries=hn, youtube_queries=yt)

    assert out.reddit_subreddits == ["startups"]
    assert len(out.hn_queries) == 1
    assert len(out.youtube_queries) == 1
```

- [ ] **Step 2: Run; expect ImportError** (`_attach_extra_source_queries` doesn't exist).
- [ ] **Step 3: Implement.** Replace `_attach_hn_queries` with the generalized helper and update imports (`YouTubeQuerySpec`):

```python
def _attach_extra_source_queries(
    plan: JobPlan,
    *,
    hn_queries: list[HackerNewsKeywordSpec],
    youtube_queries: list[YouTubeQuerySpec],
) -> JobPlan:
    """Re-attach every non-Reddit source field to a post-tail plan in ONE
    model_construct. The locked Reddit tail rebuilds the plan with only
    Reddit fields and silently drops the rest; this is the single carry-
    through point. model_construct skips validation so the post-pruning
    Reddit fields survive (the 'too few survived' case is enforced inside
    _finalize). See spec §6.

    INVARIANT: any new non-Reddit source field on JobPlan MUST be threaded
    through here (capture once in run_query_expansion, reattach once)."""
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=plan.reddit_subreddits,
        hn_queries=hn_queries,
        youtube_queries=youtube_queries,
    )
```

Update `run_query_expansion`:

```python
    hn_queries = list(raw_plan.hn_queries)            # capture once
    youtube_queries = list(raw_plan.youtube_queries)  # capture once
    grounded = _ground_selection(raw_plan, candidates)
    final_plan = _finalize(grounded, spec)
    final_plan = _attach_extra_source_queries(
        final_plan, hn_queries=hn_queries, youtube_queries=youtube_queries
    )
```

- [ ] **Step 4: Run tests + checks; expect pass.** Run the full station test file to confirm the existing grounded-flow tests still pass.
- [ ] **Step 5: Commit** (`refactor(llm): generalize Wave-0 carry-through to all non-Reddit fields`).

---

### Task 4.2: v8 prompt — Kind 4 section

**Files:**
- Modify: `src/discovery/llm/prompts/query_expansion.py` — add the Kind 4 section after Kind 3, update the master "What to emit" to four fields, add the `build_user_message` line, bump `VERSION` v7 → v8, add the v8 changelog entry.
- Test: `tests/unit/llm/test_prompts_query_expansion.py` — assert VERSION == "v8", that the prompt contains the Kind 4 markers (e.g. "youtube_queries", "complaint", "discussion", "day in the life"), and that `build_user_message` includes the youtube line.

**Spec reference:** §8.

- [ ] **Step 1: Write the failing tests** (follow the existing shape-test style in that file; assert on substrings, not full text):

```python
def test_version_is_v8() -> None:
    from discovery.llm.prompts import query_expansion
    assert query_expansion.VERSION == "v8"


def test_kind_4_section_present() -> None:
    from discovery.llm.prompts.query_expansion import SYSTEM_PROMPT
    assert "youtube_queries" in SYSTEM_PROMPT
    assert "Kind 4" in SYSTEM_PROMPT
    assert "complaint" in SYSTEM_PROMPT and "discussion" in SYSTEM_PROMPT


def test_user_message_mentions_youtube() -> None:
    from datetime import date
    from discovery.jobs import JobSpec
    from discovery.llm.prompts.query_expansion import build_user_message
    msg = build_user_message(JobSpec(industry="x", as_of=date(2026, 5, 22), time_window="month"), [])
    assert "youtube_queries" in msg
```

(Confirm the exact existing assertions in `test_prompts_query_expansion.py` and keep them green — the v7→v8 bump may require updating any test that pins the version string or counts fields.)

- [ ] **Step 2: Run; expect failure.**
- [ ] **Step 3: Implement** the Kind 4 section per spec §8 (the seven surfaces, emotion-search templates, intent tagging, "emit ~15-20, top 10 fire", graceful sparsity, one example industry with the re-derive guard), the four-field "What to emit" block, the `build_user_message` youtube line, `VERSION = "v8"`, and a `v8 — ...` changelog line in the module docstring. Keep all new strings ASCII (RUF001).
- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit** (`feat(llm): v8 prompt — Kind 4 YouTube keyword candidates`).

---

## Chunk 5: Plumbing — registry, CLI fan-out, skill, handoff

### Task 5.1: Register the YouTube adapter

**Files:**
- Modify: `src/discovery/workers/__init__.py`.
- Test: `tests/unit/workers/test_registry.py` — add `test_includes_youtube_adapter` + a constructed-without-key assertion.

**Spec reference:** §13.

- [ ] **Step 1: Write the failing tests.** Add this method INSIDE the existing `TestBuildDefaultRegistry` class (where `test_includes_hackernews_adapter` lives), so `self` is valid:

```python
    def test_includes_youtube_adapter(self) -> None:
        from discovery.sources.youtube import YouTubeSource  # noqa: PLC0415

        registry = build_default_registry()
        assert "youtube" in registry
        assert isinstance(registry["youtube"], YouTubeSource)
```

- [ ] **Step 2: Run; expect failure** (`"youtube" not in registry`).
- [ ] **Step 3: Implement** in `build_default_registry` (add the lazy import + key resolution + registration):

```python
    from discovery.sources.youtube import YouTubeSource  # noqa: PLC0415

    yt_key = (
        settings.youtube_api_key.get_secret_value()
        if settings.youtube_api_key is not None
        else None
    )
    adapters: dict[str, BaseSource] = {
        "reddit": RedditSource(user_agent=settings.reddit_user_agent),
        "hackernews": HackerNewsSource(),
        "youtube": YouTubeSource(api_key=yt_key),
    }
```

- [ ] **Step 4: Run tests + checks; expect pass.**
- [ ] **Step 5: Commit** (`feat(workers): register YouTube adapter (no-op without key)`).

---

### Task 5.2: Three-way parallel fan-out in `cli/run.py`

**Files:**
- Modify: `src/discovery/cli/run.py` — add the YouTube enqueue, capture its id, add the third `asyncio.gather` branch, update the print lines ("3 task(s) processed").
- Test: `tests/unit/test_run_cli_parallel.py` — add a `_YouTubeDouble` and a three-way concurrency test; keep the existing two-way test green.

**Spec reference:** §12.

- [ ] **Step 1: Write the failing test.** Add a `_YouTubeDouble(_RecordingBase)` with `name = "youtube"`, and:

```python
async def test_three_tasks_dispatch_concurrently(self, maker: Any) -> None:
    reddit_id = await _make_queued_task(maker, "reddit")
    hn_id = await _make_queued_task(maker, "hackernews")
    yt_id = await _make_queued_task(maker, "youtube")

    started: dict[str, float] = {}
    registry: dict[str, BaseSource] = {
        "reddit": _RedditDouble(started, 0.05),
        "hackernews": _HNDouble(started, 0.05),
        "youtube": _YouTubeDouble(started, 0.05),
    }
    t0 = time.monotonic()
    await asyncio.gather(
        _run_task_in_own_session(maker, registry, reddit_id),
        _run_task_in_own_session(maker, registry, hn_id),
        _run_task_in_own_session(maker, registry, yt_id),
    )
    wall = time.monotonic() - t0
    assert len(started) == 3
    assert wall < 0.12, f"wall={wall:.3f}s -- looks sequential"
```

(`_run_task_in_own_session` is unchanged — already task-id-generic — so this test passes once the double exists; the real change under test is `_run_discovery`'s wiring. Add a lightweight assertion or rely on the existing CLI smoke if present. If `_run_discovery` is not directly unit-tested, the wiring change is verified by the existing CLI integration path; keep this concurrency test as the regression guard.)

**Keep the existing two-way test green.** Do NOT relax `test_two_tasks_dispatch_concurrently`'s `wall < 0.09` bound — only the new three-way test gets the looser `< 0.12` bound (a third 50ms branch widens the worst case). Both must pass.

- [ ] **Step 2: Run; the new double-based test should pass once added; the wiring edit is verified by checks + the existing tests.**
- [ ] **Step 3: Implement** the `cli/run.py` edits: import `enqueue_youtube_task_for_job`; after the HN enqueue, `youtube_task = await enqueue_youtube_task_for_job(session, job)`; extend the "queued tasks" print to include `youtube={youtube_task.id} (queries={len(youtube_task.params['queries'])})`; capture `youtube_task_id`; add the third `_run_task_in_own_session(maker, registry, youtube_task_id)` to the `asyncio.gather`; change "2 task(s) processed." → "3 task(s) processed."; add `assert youtube_task_id is not None`.
- [ ] **Step 4: Run tests + checks; expect pass** (full suite — this touches the CLI).
- [ ] **Step 5: Commit** (`feat(cli): fan out to YouTube as the third concurrent source`).

---

### Task 5.3: The `youtube-source` project skill

**Files:**
- Create: `.claude/skills/youtube-source/SKILL.md` (owner-authorized — CLAUDE.md guards `.claude/`).

**Spec reference:** §14.

- [ ] **Step 1:** Write the skill following the `hackernews-source` numbered structure, items 1-12 per spec §14, plus the "Divergences from related skills" single-point-of-truth section. No tests (it's a doc).
- [ ] **Step 2:** Verify it renders (read it back) and that every item cross-references real symbols/files.
- [ ] **Step 3: Commit** (`docs(skill): add youtube-source operational playbook`).

---

### Task 5.4: Update the handoff log

**Files:**
- Modify: `docs/handoff.md` — append a "YouTube source adapter (2026-05-22) — what shipped & locked in" section (newest-first, mirroring the HN section), update the commit-history table and the "what's NOT built yet" / "pieces that exist" map, and the test-count line.

- [ ] **Step 1:** Write the section: problem solved, new pieces, decisions locked in (MAX_YT_QUERIES=10, COMMENT_TOP_K=50, enrich-with-stats, generalized carry-through, quota-aware retry, dedicated key, two Bronze kinds), and a provisional smoke-verified placeholder (the real-key smoke run happens after the user creates the key).
- [ ] **Step 2: Commit** (`docs(handoff): record the YouTube source slice`).

---

## Final verification (before finishing the branch)

- [ ] Full green suite in WSL:

```
wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src/"
```

(Allow only the known pre-existing WSL-only `test_settings.py::TestFindProjectRoot::test_windows_style_worktree_path` failure.)

- [ ] **Real smoke run (after the user adds `YOUTUBE_API_KEY` to `.env`).** Use a NEW industry so the Wave-0 cache and per-job idempotency don't short-circuit:

```
wsl bash -c "cd /mnt/c/Users/skyto/pain_points_poject && uv run discovery run --industry 'mobile dog grooming' --location US --time-window year"
```

Expect: `wave 0: planned`, three queued tasks (reddit/hackernews/youtube), `3 task(s) processed`, and `raw_records` rows with `source='youtube'` of both `kind`s. If `YOUTUBE_API_KEY` is unset, expect the YouTube task to complete `done` with zero rows (graceful no-op) — not `failed`.

- [ ] Use @superpowers:finishing-a-development-branch to merge.

---

## Review checkpoints (per the writing-plans review loop)

Each chunk above is independently reviewable. After implementing each chunk, dispatch a fresh code reviewer against the chunk + the spec before moving on (per @superpowers:subagent-driven-development's two-stage review). Pay special attention to: the carry-through preserving BOTH non-Reddit fields (Chunk 4), the quota-stop NOT attempting later queries (Chunk 2 Task 2.4), and the no-key no-op path (Chunk 2 Task 2.4).
