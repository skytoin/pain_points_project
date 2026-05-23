# YouTube source adapter — design

**Date:** 2026-05-22
**Status:** approved (brainstorm phase) — pending spec review + user sign-off
**Author:** Claude Opus 4.7 (1M context), in session with the project owner

---

## 1. Goal

Add a third Wave-1 source to the discovery pipeline: YouTube, via the
YouTube Data API v3. After this slice ships, every `discovery run` fans
out to **Reddit, HackerNews, AND YouTube concurrently** in a single job,
so the wall-clock time of a job is `max(reddit, hn, youtube)`, not the
sum. Wave 0 (the existing OpenAI gpt-5.4 query expansion station) is
taught to emit a fourth output, `youtube_queries`, alongside the existing
`reddit_queries`, `reddit_subreddits`, and `hn_queries`. Bronze
(`raw_records`) gains YouTube rows with `source="youtube"`, stored
verbatim. Nothing about how Reddit or HN works changes — both existing
code paths are untouched.

YouTube is, for this pipeline, a **pain surface**: the richest signal is
not the videos themselves but the comments under them (people saying "I
followed this but X breaks"), plus whole genres of pain-monologue video
("why I quit X", "X horror stories"). The adapter therefore does a
three-step fetch — search, then enrich with statistics, then harvest
comments from the highest-view videos — and stores both video resources
and comment threads verbatim into Bronze.

## 2. Non-goals

- **Wave 2 (pain classification) is out of scope.** Bronze YouTube rows
  stay unclassified until Wave 2 lands as its own slice. Routing on the
  YouTube resource `kind` field (`youtube#video` vs
  `youtube#commentThread`), pain extraction, tool extraction, and
  job-task mining are all Wave 2 concerns. This slice's contract is the
  project-locked *Bronze stores raw, Wave 2 parses*.
- **No video transcript / caption fetching.** Captions need a separate
  (often OAuth-gated, quota-heavier) API surface. The signal we capture
  is title + description + statistics (from `videos.list`) and the
  comment threads. Transcripts are a possible future slice, not this one.
- **No `regionCode` / `relevanceLanguage` mapping from `JobSpec`.**
  `JobSpec.location` is free-form (e.g. `"NY"`, `"US"`, `"the
  Midwest"`) and does not cleanly map to the API's required ISO 3166-1
  alpha-2 `regionCode` or ISO 639-1 `relevanceLanguage`. A bad mapping
  silently narrows results, which is worse than omitting it. Both params
  are omitted in this slice; revisit if a future slice adds a validated
  locale mapping.
- **No `order` routing per intent.** Given enrichment captures view
  statistics into Bronze (so downstream can rank by demand magnitude)
  and `publishedAfter` already bounds recency, every query uses
  `order=relevance` — the highest-precision choice for a scarce
  10-query budget. The `intent` tag drives LLM generation balance and
  downstream tagging, NOT API parameters (see §8, §9).
- **No comment replies / pagination.** One `commentThreads.list` call
  per harvested video (up to 100 top-level comments, 1 unit). No
  `pageToken` walking, no `comments.list` reply expansion. The top
  comments hold the signal; deep pagination burns quota for diminishing
  returns.
- **No multi-worker concurrency lift.** CLAUDE.md's single-worker
  assumption stays. Per-job parallelism across the three sources is
  achieved by directly dispatching the three known task ids in
  `cli/run.py` via the existing race-safe `claim_known_task`, exactly as
  the HN slice established. `claim_one` is untouched.
- **No CLI flag work.** All three sources run every job; no `--only`, no
  `--with-youtube`. Consistent with the HN slice's "Both, every run"
  decision, now "All three, every run".
- **No DRY extraction of the time-window helper.** Reddit, HN, and now
  YouTube each compute a time-window floor in a slightly different
  output format (Reddit's coarse `t` bucket, HN's unix epoch int,
  YouTube's RFC 3339 string). They share intent but not output type.
  Defer any extraction to a future slice; it would be its own brainstorm.

## 3. Background

**Why YouTube now.** Reddit (pain/complaints) and HN (capability/launches)
are built. YouTube adds a third, distinct surface: practitioner pain
expressed in video and especially in comments — tutorials whose comments
are full of "this breaks for me", review comments listing the cons the
reviewer missed, "day in the life" videos that reveal manual workarounds,
and pure pain-monologue genres ("why I quit X", "X horror stories",
"things nobody tells you about X").

**The seven surfaces where pain hides on YouTube** (owner's domain
knowledge, drives the Kind 4 prompt):

1. Comments under tutorials — literal pain in plain English ("I followed
   this exactly but Y breaks"). The single best surface.
2. "Why I quit [X]" videos — pure pain monologues.
3. Review videos and their comments — explicit cons, plus the cons the
   reviewer missed surfacing in comments.
4. "Day in the life of [profession]" — visible friction (the 5 apps, the
   manual data entry, the workarounds).
5. "Things I wish I knew before [becoming X]" — retrospective pain.
6. Storytime / horror-story videos — pain compiled across many people.
7. Live Q&A / AMAs — pain verbalized in real time.

**Query patterns that surface pain** (taught in the prompt, re-derived
per industry, never copied): `why I quit {role}`, `{tool} sucks/broken`,
`things nobody tells you about {profession}`, `{role} horror stories`,
`worst part of being {profession}`, `{tool A} vs {tool B}`, `day in the
life {profession}`, `I hate {thing}` / `rant about {thing}`, `{industry}
tips` (the comments under tip videos are people asking what they struggle
with). The "vs" pattern is especially strong: comparison-video comments
are people explaining which tool failed them and why.

**What's locked from prior slices we must respect.**

- The Wave-0 station's *deterministic Reddit tail is UNCHANGED and
  order-preserved*. Four `JobPlan.model_construct(reddit_queries=...,
  reddit_subreddits=...)` rebuilds inside the station MUST stay
  Reddit-only. The HN slice carries `hn_queries` across this tail via a
  single helper; this slice **generalizes that helper** to carry
  `youtube_queries` too (see §6).
- `JobPlan.reddit_queries` band stays 25–30. `MIN_VALID_QUERIES=10`
  (decoupled Reddit floor) stays.
- `JobPlan.hn_queries` permissive default (no `min_length`) stays;
  `youtube_queries` gets the same treatment for the same reason.
- Single-worker assumption — `claim_one` is race-unsafe under concurrent
  callers — stays. Parallel dispatch routes around it via
  `claim_known_task` (already exists, additive, untouched here).
- `raw_records.body` stores raw API responses verbatim; Wave 2 parses.
- Wave 0 brainstorms; Python validates. Creativity in the prompt,
  exactness in tested code (Approach A).
- `(source, external_id)` is UNIQUE on `raw_records`. Verbatim YouTube
  resources dedup by their id (videoId for videos, comment id for
  comment threads — these never collide).
- `JobPlan` is `extra="allow"`, but app code must NOT read from
  `model_extra`; add a typed field and read from that.

**The Reddit/HN/YouTube asymmetry.** Reddit rewards pain/frustration
phrasings. HN rewards capability/launch phrasings. YouTube rewards
*emotion-shaped* searches that land on pain-monologue videos and
comment-rich tutorial/review videos. These are three genuinely different
keyword styles — the v8 prompt teaches YouTube's own construction
principles, not a port of either sibling.

## 4. YouTube Data API v3 — facts the design depends on

Researched against the live official docs (developers.google.com/youtube/v3)
on 2026-05-22.

- **Auth.** A plain **API key** (Google Cloud project with YouTube Data
  API v3 enabled) is sufficient for read-only access to public data —
  `search.list`, `videos.list`, and `commentThreads.list` on public
  content. No OAuth needed (OAuth is only for private user data or
  writes). The key is passed as a `key=` query parameter.
- **Quota — the load-bearing constraint.** Default allocation is
  **10,000 units/day per project**, reset daily. `search.list` costs
  **100 units/call**; `videos.list` and `commentThreads.list` cost **1
  unit/call** each. Every request, even an invalid one, costs >=1 unit.
  This is the opposite of HN (effectively unlimited): YouTube forces a
  small, expensive search budget. Quota cost table:
  developers.google.com/youtube/v3/determine_quota_cost. (The exact
  reset time — believed midnight Pacific — is not stated on the YouTube
  v3 docs; treat as "daily", confirm in Cloud Console if it matters.)
- **`search.list` params used.** `part=snippet`, `q` (full-text;
  supports `|` OR and `-` NOT operators — NOT strict token-AND, so no
  decomposition is needed), `type=video`, `order=relevance`,
  `publishedAfter` (RFC 3339, e.g. `2026-05-01T00:00:00Z`),
  `maxResults=50` (the max). Endpoint:
  `https://www.googleapis.com/youtube/v3/search`.
- **`search.list` response.** `items[]`, each with `id.videoId` and a
  `snippet` (`title`, `description` — truncated, `channelTitle`,
  `channelId`, `publishedAt`, `thumbnails`). **No engagement statistics
  in search results** — that is why step 2 exists.
- **`videos.list` (enrichment).** `part=snippet,statistics`, `id=` a
  comma-separated list of up to **50** video ids per call (1 unit).
  Returns a video resource per id with full `snippet` and a
  `statistics` block (`viewCount`, `likeCount`, `commentCount` — all
  returned as strings; `favoriteCount` deprecated/0; `dislikeCount`
  private). Endpoint:
  `https://www.googleapis.com/youtube/v3/videos`.
- **`commentThreads.list` (comment harvest).** `part=snippet`,
  `videoId=`, `order=relevance`, `maxResults=100` (1 unit). Returns up
  to 100 top-level comment threads; each thread's body carries
  `snippet.videoId` (so the video link survives verbatim). A video with
  comments disabled returns **HTTP 403 `commentsDisabled`** — a
  per-video skip, NOT a quota signal. Endpoint:
  `https://www.googleapis.com/youtube/v3/commentThreads`.
- **Error / quota handling.** `quotaExceeded` / `dailyLimitExceeded`
  (HTTP 403) mean the daily budget is gone — retrying is pointless until
  reset and each retry still costs a unit. `rateLimitExceeded` /
  `userRateLimitExceeded` (HTTP 403/429) mean too-fast — retryable with
  backoff. `commentsDisabled` (403) is a benign per-video condition.

**Quota budget for one job** (with this slice's caps):

| Step | Calls | Units |
|------|-------|-------|
| `search.list` | up to 10 | up to 1,000 |
| `videos.list` (enrich, <=50 ids/call) | ~10 | ~10 |
| `commentThreads.list` (top 50 videos) | up to 50 | up to 50 |
| **Total** | | **~1,060** |

So roughly **9 jobs/day** on the default quota. Re-running the exact
same spec hits the Wave-0 cache and the per-job idempotency, costing no
new quota; each distinct `--industry` / `--as-of` is a fresh job.

## 5. Architecture overview

```
                       discovery run
                             |
                             v
                       create_job(spec)
                             |
                             v
                   plan_job (Wave 0, inline)
                             |
              run_query_expansion(spec) -> JobPlan
              |-- existing grounded Reddit chain (UNCHANGED tail)
              |-- hn_queries carried across the Reddit tail
              |-- NEW: youtube_queries carried across the SAME tail
              |        via the now-GENERALIZED carry-through helper
              |        (capture hn+youtube once, reattach both once)
                             |
                       Job.job_plan <- plan.model_dump()
                             |
        +--------------------+--------------------+
        v                    v                    v
 enqueue_reddit_task  enqueue_hn_task    enqueue_youtube_task
        |                    |                    |
        +--------- asyncio.gather (3 branches) ---+
                             |       (cli/run.py; each branch
                             v        its own session; claim_known_task)
              RedditSource | HackerNewsSource | YouTubeSource
                             |
                             v
                       raw_records
              (source in {reddit, hackernews, youtube})
```

Wave 0 failure path unchanged: `QueryExpansionError` → `job.job_plan`
stays null → every orchestrator detects null and falls back to its
deterministic no-LLM template. All three sources still run; all three
still produce Bronze rows (or zero rows, gracefully).

YouTube-specific degradation: if `YOUTUBE_API_KEY` is unset, the adapter
no-ops (returns `[]`, logs once) and the task completes `done` with zero
rows — the same graceful shape as HN sparsity on non-tech industries.

## 6. Wave-0 station — generalizing the carry-through

**Finding (inherited from the HN slice).** The deterministic Reddit tail
in `src/discovery/llm/stations/query_expansion.py` rebuilds the plan with
`JobPlan.model_construct(reddit_queries=..., reddit_subreddits=...)` at
four sites (`_ground_selection`, `_force_time_window`,
`_merge_baseline_subreddits`, `_drop_invalid_queries`). `model_construct`
keeps only the fields passed, silently dropping every non-Reddit field.
The HN slice handled this with `_attach_hn_queries`, called once after
the tail.

**The trap this slice must avoid.** You cannot add a second
`_attach_youtube_queries` and chain it after `_attach_hn_queries`: the
second `model_construct` would re-drop `hn_queries` (only-fields-passed
survive). A copy approach forces each helper to list *every* non-Reddit
field anyway — fragile and order-dependent.

**Fix — generalize to one helper.** Replace `_attach_hn_queries` with a
single helper that reattaches all non-Reddit source fields in one
`model_construct`:

```python
def _attach_extra_source_queries(
    plan: JobPlan,
    *,
    hn_queries: list[HackerNewsKeywordSpec],
    youtube_queries: list[YouTubeQuerySpec],
) -> JobPlan:
    """Re-attach every non-Reddit source field to a post-tail plan in a
    single model_construct. The locked Reddit tail rebuilds the plan
    with only Reddit fields and silently drops the rest; this helper is
    the single carry-through point. Uses model_construct (skips
    validation) so the post-pruning Reddit fields survive; the "too few
    survived" case is enforced inside _finalize.
    """
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=plan.reddit_subreddits,
        hn_queries=hn_queries,
        youtube_queries=youtube_queries,
    )
```

Wiring in `run_query_expansion` — capture both once after the LLM call,
let the locked tail run untouched, reattach both once before caching:

```python
raw_plan = await _select_and_design(spec, candidates)

hn_queries = list(raw_plan.hn_queries)            # capture once
youtube_queries = list(raw_plan.youtube_queries)  # capture once
grounded = _ground_selection(raw_plan, candidates)
final_plan = _finalize(grounded, spec)
final_plan = _attach_extra_source_queries(         # reattach once
    final_plan, hn_queries=hn_queries, youtube_queries=youtube_queries
)
put_cached(_cache, key, final_plan)
return final_plan
```

**Invariant for future sessions.** Any new non-Reddit source field added
to `JobPlan` MUST be threaded through `_attach_extra_source_queries`
(capture once, reattach once). The four locked tail helpers stay
byte-for-byte Reddit-only. The carry-through is fragile by design and
this single helper is its only correct home.

## 7. Schema additions (`src/discovery/llm/schemas.py`)

```python
class YouTubeQuerySpec(BaseModel):
    """Wave 0 LLM YouTube search candidate. Python downstream normalizes,
    dedupes, applies the time-window publishedAfter floor, caps at
    MAX_YT_QUERIES, and runs the three-step fetch. See
    docs/specs/2026-05-22-youtube-source-design.md sections 8-10.
    """

    model_config = ConfigDict(frozen=True)

    query: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "Full-text YouTube search phrase, emotion/pain-shaped and "
            "re-derived for THIS industry (e.g. 'why I quit commercial "
            "cleaning', 'Jobber vs Housecall Pro'). Used near-verbatim "
            "as the `q` parameter; YouTube is full-text relevance "
            "search, NOT token-AND, so no decomposition is applied."
        ),
    )
    intent: Literal["complaint", "discussion"] = Field(
        description=(
            "complaint -> the video itself is the pain (why-I-quit, "
            "horror stories, rant, worst-part, wish-I-knew). "
            "discussion -> the pain is in the comments and the video "
            "reveals tools/workflows (tutorials, tips, reviews, A-vs-B, "
            "day-in-the-life). Used for LLM generation balance and "
            "downstream Wave 2 tagging; does NOT route API params."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this YouTube candidate is worth running.",
    )


class JobPlan(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    reddit_queries: list[RedditQuerySpec] = Field(min_length=25, max_length=30)
    reddit_subreddits: list[str] = Field(default_factory=list)
    hn_queries: list[HackerNewsKeywordSpec] = Field(default_factory=list)
    youtube_queries: list[YouTubeQuerySpec] = Field(default_factory=list)  # NEW
```

**Permissive default (no `min_length`) is deliberate** — identical
rationale to `hn_queries`: a strict floor could let YouTube
under-production raise `QueryExpansionError` and sink the Reddit grounded
plan. YouTube sparsity must degrade gracefully (empty list → no-op task
→ done with zero rows). Mirrors `hn_queries`.

## 8. v8 prompt — adding Kind 4 (`src/discovery/llm/prompts/query_expansion.py`)

`VERSION` bumps `"v7"` → `"v8"`, which invalidates the combined Wave-0
cache (keyed `f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}"`).
The existing Kind 1/2 (Reddit) and Kind 3 (HN) sections are untouched. A
new top-level **Kind 4** section is added after Kind 3; the master "What
to emit" section moves from three fields to four; `build_user_message`
gains one line.

The Kind 4 section teaches:

- **The seven surfaces** (§3) as the targets the searches should land on.
- **The emotion-search templates** (§3) as the query shapes, with an
  explicit "re-derive for THIS industry, never copy" guard (same guard
  the wedding-photography and personal-CRM illustrations use).
- **`intent` tagging** — `complaint` (the video is the pain) vs
  `discussion` (the pain is in the comments; the video reveals tools).
  Aim for a balanced mix (target ~half and half; Python does NOT enforce
  a ratio — `intent` does not route API params, it balances generation
  and tags downstream).
- **Emit ~15–20 candidates, strongest first.** Python dedupes and caps
  the fired set at `MAX_YT_QUERIES=10` in the LLM's emitted order, so
  ordering is a ranking signal — best candidates in the first ~10
  positions. Emit more than the cap so post-dedup survivors fill it.
- **Graceful sparsity** — for industries with weak YouTube coverage,
  emit fewer or zero rather than manufacturing generic phrases. Quality
  over quota; the downstream is fine with an empty list.
- **One example industry illustration** (e.g. "mobile dog grooming")
  showing pain-shaped phrases tagged by intent, with the re-derive
  guard.

The master "What to emit" section becomes four fields:

```
- reddit_queries     — 25-30 RedditQuerySpec (Kinds 1 & 2).
- reddit_subreddits  — shortlist of domain-relevant subs.
- hn_queries         — 15-20 HackerNewsKeywordSpec (Kind 3).
- youtube_queries    — 15-20 YouTubeQuerySpec (Kind 4). Re-derive
                       emotion/pain-shaped angles for THIS industry;
                       do NOT translate the reddit/hn queries to YouTube.
```

`build_user_message` adds one line near the closing instruction:

```
Plus 15-20 youtube_queries: YouTube search candidates re-derived for
THIS industry (emotion/pain-shaped: why-I-quit, horror stories, A-vs-B,
day-in-the-life, tutorial/tips whose comments hold pain). Tag intent
(complaint or discussion) per candidate; aim for a balanced mix.
```

**Why this respects Approach A.** The mechanical rules (the RFC 3339
window, `order=relevance`, dedup, the 10-cap, the three-step fetch, the
top-50 comment harvest) are NOT in the prompt — they are tested Python
(§9, §10). What IS in the prompt is what the LLM uniquely judges: which
emotion-shaped searches are YouTube-suitable for this industry, in what
shape, with what intent.

## 9. YouTube orchestrator (`src/discovery/orchestrator/youtube.py`)

Mirrors `orchestrator/hackernews.py`. Public surface:

- `enqueue_youtube_task_for_job(session, job) -> Task` — idempotent on
  `content_hash`, mirrors `enqueue_hn_task_for_job`.
- `youtube_queries_for_spec(spec) -> list[dict]` — the no-LLM template
  fallback.

Internal helpers (each one job, <=60 lines per CLAUDE.md):

- `_queries_from_job_plan(job) -> list[dict] | None` — validates the
  stored `JobPlan`; returns `None` (template fallback) when `job_plan`
  is null or fails validation; returns `[]` (graceful sparsity, NOT
  template) when valid but `youtube_queries` is empty; otherwise the
  compiled list.
- `_compile_yt_queries(specs, job_spec) -> list[dict]` — normalize each
  `query` (strip + collapse internal whitespace), drop empties, dedup on
  the lowercased normalized query, build the fetch-params dict, cap at
  `MAX_YT_QUERIES=10` preserving LLM order.
- `_time_window_rfc3339(time_window, as_of) -> str | None` — the
  JobSpec time-window → RFC 3339 floor string. `all` → `None` (omit
  `publishedAfter`). Same offset table as HN's `_time_window_epoch`,
  formatted as `YYYY-MM-DDTHH:MM:SSZ`.
- `_build_fetch_params(query, published_after) -> dict` — the per-query
  dict the adapter consumes.

**Constants.** `MAX_YT_QUERIES = 10` (the search budget cap — the
load-bearing number, owner-chosen against the ~9-jobs/day quota math).
`COMMENT_TOP_K = 50` lives in the **adapter** (§10), not here — it is a
fetch-time concern, not a query-planning concern, and the orchestrator
emits the same param dict regardless.

**Time-window → RFC 3339 floor:**

| `time_window` | offset | example output (as_of=2026-05-22) |
|---------------|--------|-----------------------------------|
| `hour`  | 3,600 s    | `2026-05-21T23:00:00Z` |
| `day`   | 86,400 s   | `2026-05-21T00:00:00Z` |
| `week`  | 604,800 s  | `2026-05-15T00:00:00Z` |
| `month` | 2,592,000 s (30 d) | `2026-04-22T00:00:00Z` |
| `year`  | 31,536,000 s (365 d) | `2025-05-22T00:00:00Z` |
| `all`   | omit `publishedAfter` entirely | (none) |

Anchor: `as_of` at midnight UTC, same as HN. `floor =
datetime.combine(as_of, time.min, tzinfo=UTC) - timedelta(seconds=OFFSET)`;
emit `floor.strftime("%Y-%m-%dT%H:%M:%SZ")` (or `isoformat()` normalized
to a `Z` suffix).

**Compiled fetch-params dict shape** (consumed by `YouTubeSource.fetch`):

```python
{
    "query": "why I quit commercial cleaning",  # near-verbatim q
    "order": "relevance",
    "type": "video",
    "part": "snippet",
    "published_after": "2026-04-22T00:00:00Z" | None,
    "max_results": 50,
}
```

`order`, `type`, `part`, `max_results` are constant in this slice but
carried in the dict (not hard-coded in the adapter) for the same
testability reason the HN dict carries its transport flag: planning
decides the shape, the adapter builds the URL.

**Task `params` envelope.** `enqueue_youtube_task_for_job` wraps the
compiled `list[dict]` exactly like HN: `params = {"queries": queries}`.
The adapter reads `params["queries"]` (§10 step 1) and the CLI Phase-1
print line reads `len(youtube_task.params["queries"])` (§12). This is the
single interface between the orchestrator and the adapter — both sides
agree on the `"queries"` key.

**Template fallback** (`youtube_queries_for_spec`) — a no-LLM fallback so
YouTube works with `OPENAI_API_KEY` unset, exactly like Reddit/HN. It
emits a small deterministic pain-shaped set from the industry literal and
runs the same compile path:

```python
def youtube_queries_for_spec(spec: JobSpec) -> list[dict[str, Any]]:
    industry = spec.industry
    candidates = [
        YouTubeQuerySpec(query=f"why I quit {industry}", intent="complaint",
                         rationale="(template) quit-the-industry pain monologue"),
        YouTubeQuerySpec(query=f"{industry} horror stories", intent="complaint",
                         rationale="(template) compiled pain across many people"),
        YouTubeQuerySpec(query=f"things nobody tells you about {industry}",
                         intent="complaint",
                         rationale="(template) retrospective pain"),
        YouTubeQuerySpec(query=f"{industry} tutorial", intent="discussion",
                         rationale="(template) comments hold 'this breaks for me' pain"),
        YouTubeQuerySpec(query=f"day in the life {industry}", intent="discussion",
                         rationale="(template) visible workflow friction"),
    ]
    return _compile_yt_queries(candidates, spec)
```

**Idempotency.** `enqueue_youtube_task_for_job` uses
`hash_params({"source": "youtube", "action": "fetch", "params": params})`
as `content_hash` and short-circuits on `(job_id, content_hash)`
collision — identical to Reddit/HN. An empty compiled list is intentional
(graceful sparsity) and still enqueues a task.

## 10. YouTube source adapter (`src/discovery/sources/youtube.py`)

```python
class YouTubeSource(BaseSource):
    name = "youtube"
    rate_limit = (5, 1)   # 5 req/s polite; quota (not rate) is the real ceiling
```

**Constructor** mirrors `HackerNewsSource`, plus an `api_key`:

```python
def __init__(
    self,
    *,
    api_key: str | None,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
    timeout: float = 30.0,
) -> None:
    self._api_key = api_key
    self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
    self._owned_client = client is None
    self._limiter = limiter if limiter is not None else AsyncLimiter(max_rate=5, time_period=1)
```

Per-instance limiter (one consumer, like HN). `aclose()` closes the
owned client (so `filterwarnings=["error"]` doesn't fail on an unclosed
client; `aclose_registry` already iterates).

**Constants.** `COMMENT_TOP_K = 50` (videos to harvest comments from,
ranked by `viewCount`). `VIDEOS_BATCH = 50` (max ids per `videos.list`).

**Pure helpers:**

- `build_search_url(query, api_key) -> str` — `.../search` with
  `part`, `q`, `type`, `order`, `maxResults`, `publishedAfter` (omitted
  when `published_after` is `None`), `key`.
- `build_videos_url(video_ids, api_key) -> str` — `.../videos?part=snippet,statistics&id=<csv>&key=`.
- `build_comments_url(video_id, api_key) -> str` —
  `.../commentThreads?part=snippet&videoId=<id>&order=relevance&maxResults=100&key=`.
- `extract_video_ids(search_payload) -> list[str]` — pull
  `item["id"]["videoId"]` from `items`, skipping items without one
  (channel/playlist results, defensive even though `type=video`).
- `video_to_raw_record(video) -> RawRecord` — `source="youtube"`,
  `external_id=str(video["id"])`, `body=video` verbatim
  (`kind=youtube#video`).
- `comment_to_raw_record(thread) -> RawRecord` — `source="youtube"`,
  `external_id=str(thread["id"])`, `body=thread` verbatim
  (`kind=youtube#commentThread`; carries `snippet.videoId`).
- `search_hit_to_raw_record(item) -> RawRecord` — fallback only (see
  step 2 enrichment quota-stop): `external_id=str(item["id"]["videoId"])`,
  `body=item` verbatim (`kind=youtube#searchResult`).
- `viewcount_of(video) -> int` — parse `statistics.viewCount` (string)
  to int, defaulting to 0 when absent (live/upcoming videos can lack it).

**Quota-aware request primitive.** A single `_get_json(url)` wrapped so:

- Transient failures (`httpx.TimeoutException`, network errors, HTTP 5xx,
  and 403/429 with reason `rateLimitExceeded` / `userRateLimitExceeded`)
  → retried with bounded exponential backoff, max 3 attempts.
- HTTP 403 with reason `quotaExceeded` / `dailyLimitExceeded` → raised as
  a dedicated `YouTubeQuotaExceeded` exception, NOT retried.
- HTTP 403 with reason `commentsDisabled` → raised as a dedicated
  `CommentsDisabled` exception (caller skips that one video).
- The reason is read from the JSON error body
  (`error.errors[0].reason`); a 403 whose reason can't be parsed is
  treated as non-retryable (fail that call) rather than retried into a
  possible wall.

**Retry is hand-rolled with an injectable `sleep`, NOT tenacity** —
mirroring `RedditSource._fetch_with_retries`. The source layer has no
tenacity (Reddit hand-rolls; HN has no retry), and the injectable-sleep
pattern is what lets tests exercise the backoff path without real waits
under `filterwarnings=["error"]`. The constructor takes
`sleep: Callable[[float], Awaitable[None]] = asyncio.sleep` exactly like
Reddit. (This refines the earlier "tenacity" wording to match the
codebase; behavior — retry transient/rate-limit, stop on quota, max 3 —
is unchanged.)

**Classification happens INSIDE the retry loop, before the retry
decision.** `_get_json` inspects the 403 reason and raises the right
exception class; the loop retries ONLY the transient classes
(`httpx.TimeoutException`, `httpx.TransportError`, HTTP-5xx, and a
`YouTubeRateLimited` class). `YouTubeQuotaExceeded` and `CommentsDisabled`
are raised straight out of the loop and never retried. Getting this
boundary backwards (classifying after the retry decision) would retry a
quota 403 three times, burning 3 wasted units; the plan keeps the raise
inside the wrapped call.

**`fetch(params) -> list[RawRecord]`** — the three-step flow. Because the
flow has five branches and CLAUDE.md caps functions at 60 lines, `fetch`
is a thin orchestrator over three named helpers (each one job, each
independently testable):

- `_search_all(queries) -> tuple[list[str], dict[str, dict]]` — runs the
  search loop and returns `(ordered_unique_video_ids, items_by_id)`.
  **It retains the raw search `items` keyed by videoId, not just the
  ids** — the enrichment fallback (below) needs them. Handles the
  quota-stop (abandon remaining queries) and per-query partial success.
- `_enrich_videos(video_ids, items_by_id) -> tuple[list[RawRecord], list[dict]]`
  — batches ids into `videos.list`, returns `(records, enriched_video_resources)`.
  On `YouTubeQuotaExceeded` it stops and, for the ids not yet enriched,
  emits `search_hit_to_raw_record` from the retained `items_by_id` so
  discovery isn't lost.
- `_harvest_comments(enriched_videos) -> list[RawRecord]` — ranks by
  `viewcount_of` desc, takes the top `COMMENT_TOP_K`, harvests each via
  `commentThreads.list`, skipping `CommentsDisabled` videos and stopping
  on `YouTubeQuotaExceeded`.

```
fetch(params):
  0. If self._api_key is None: log once, return [].
  1. ids, items_by_id = _search_all(params["queries"])
       SEARCH loop: for each query, _get_json(build_search_url(...));
       extract_video_ids AND retain the raw item under items_by_id[videoId].
       - YouTubeQuotaExceeded -> stop the loop (remaining queries would
         hit the same wall). Keep ids/items gathered so far.
       - other (httpx.HTTPError, ValueError) -> record error, continue
         (partial success). Dedup ids preserving first-seen order.
     If no ids and there were search errors -> raise the first error
     (task fails). If no ids and no errors -> return [].
  2. video_records, enriched = _enrich_videos(ids, items_by_id)
       Batch ids into <=50-id videos.list calls; one video_to_raw_record
       per enriched video.
       - YouTubeQuotaExceeded -> stop enriching. FALLBACK: for ids not
         yet enriched, emit search_hit_to_raw_record from items_by_id so
         discovery isn't lost. (enriched stays partial; comment step gets
         only what was enriched.)
  3. comment_records = _harvest_comments(enriched)
       Rank enriched by viewcount_of desc; take top COMMENT_TOP_K; for
       each, _get_json(build_comments_url(...)); comment_to_raw_record
       per thread.
       - CommentsDisabled -> skip this video, continue.
       - YouTubeQuotaExceeded -> stop the loop, keep what we have.
  4. return video_records + comment_records
       (video_records already includes any search-hit fallback records
       from step 2.)
```

Videos missing `statistics.viewCount` (live/upcoming) get `viewcount_of`
= 0 and sort to the bottom of the comment-harvest ranking — intended:
they are the least valuable comment targets.

**Partial success contract** (project-locked, same as Reddit/HN): a job
that gets *some* records returns them; only a total wipeout where the
search step gathered nothing and errored re-raises so the worker marks
the task failed. A `quotaExceeded` mid-job is a clean partial stop, not a
task failure.

**File-size note.** This adapter is heavier than HN's 175-line one (7
pure helpers + constructor + `aclose` + `_get_json` with the hand-rolled
retry loop and JSON-error-reason parsing + 2 custom exception classes + the
3 fetch helpers + docstrings) — estimated ~350-450 lines, under the
600-line cap. If the implementation crosses ~500 lines, split the pure
helpers (URL builders, record converters, `extract_video_ids`,
`viewcount_of`, the exception classes) into a sibling pure module
`src/discovery/sources/youtube_helpers.py`, mirroring how HN factored
`keyword_tokens.py`. The plan should size the file at the end of Chunk 2
and split then if needed, rather than discovering it mid-implementation.

**Per-call log lines** (skill-21 analog): each search / enrich / comment
call logs URL (with the key redacted), status, elapsed_ms, and counts
(ids found, videos enriched, comment threads harvested). The key is
NEVER logged in clear.

## 11. Config (`src/discovery/config/settings.py`)

Add one optional field next to the other source keys:

```python
youtube_api_key: SecretStr | None = None
```

Optional, defaults to `None`, mirrors every other source credential.
When unset the adapter no-ops (§10 step 0). No other settings change.

Note: `settings.py` already defines an unused `google_api_key` field. It
is **intentionally not reused** — a dedicated `youtube_api_key` keeps
YouTube separable from other Google services (e.g. a future Google Places
source) that would want their own key. Owner decision (§18).

## 12. Parallel fan-out (`src/discovery/cli/run.py`)

The HN slice already established the three-phase structure (setup /
parallel dispatch / report) and the `claim_known_task` route-around. This
slice adds the third branch:

- Phase 1: after `enqueue_reddit_task_for_job` and
  `enqueue_hn_task_for_job`, add `enqueue_youtube_task_for_job`; capture
  `youtube_task.id`; extend the "queued tasks" print line.
- Phase 2: add a third arg to `asyncio.gather`:
  `_run_task_in_own_session(maker, registry, youtube_task_id)`. The
  "done. N task(s) processed." line becomes 3.
- `_run_task_in_own_session` is unchanged (it is task-id-generic).

**Partial success across sources** is automatic and unchanged: each
branch opens its own session, `run_one` catches and finalizes adapter
failures internally, and `asyncio.gather` runs all three to completion.
One source dying never kills the others.

## 13. Registry wiring (`src/discovery/workers/__init__.py`)

```python
def build_default_registry() -> SourceRegistry:
    from discovery.config.settings import settings
    from discovery.sources.hackernews import HackerNewsSource
    from discovery.sources.reddit import RedditSource
    from discovery.sources.youtube import YouTubeSource  # NEW

    yt_key = (
        settings.youtube_api_key.get_secret_value()
        if settings.youtube_api_key is not None
        else None
    )
    adapters: dict[str, BaseSource] = {
        "reddit": RedditSource(user_agent=settings.reddit_user_agent),
        "hackernews": HackerNewsSource(),
        "youtube": YouTubeSource(api_key=yt_key),  # NEW — no-ops if key is None
    }
    return adapters
```

`aclose_registry` already iterates and calls `aclose`; `YouTubeSource.aclose`
closes the owned client — no `aclose_registry` change needed.

## 14. The new project skill (`.claude/skills/youtube-source/SKILL.md`)

The owner explicitly authorized this skill (CLAUDE.md normally guards
`.claude/`). Numbered like the `reddit-source` / `hackernews-source`
skills so reviews and commits can cross-reference items by number.
Outline:

1. **Use the Data API v3 with an API key.** No OAuth for public
   read-only. Key in `settings.youtube_api_key`; no-op when unset.
2. **Quota is the harshest constraint.** 10,000 units/day; `search.list`
   = 100, `videos.list` / `commentThreads.list` = 1. Budget around
   searches. `MAX_YT_QUERIES=10`.
3. **Three-step fetch:** search → enrich (`videos.list` stats) → harvest
   comments (`commentThreads.list`, top `COMMENT_TOP_K=50` by views).
4. **YouTube is full-text, not token-AND.** No decomposition; the `q`
   phrase passes through near-verbatim. (`|` OR and `-` NOT supported.)
5. **Comments are the best pain surface.** The seven surfaces; comments
   under tutorials/reviews/tips; pain-monologue genres.
6. **`order=relevance` always; `publishedAfter` from the time window.**
   `intent` does NOT route API params — it balances generation and tags
   downstream.
7. **Quota-aware retry.** Retry transient + rateLimit; STOP on
   quotaExceeded/dailyLimitExceeded (never retry into the wall); skip
   commentsDisabled per-video.
8. **Bronze stores raw, two kinds.** `youtube#video` and
   `youtube#commentThread` under `source="youtube"`, distinguished by
   the `kind` field. Verbatim; Wave 2 parses. (Rare `youtube#searchResult`
   fallback when enrichment quota-stops.)
9. **Per-instance limiter, not a singleton** (one consumer).
10. **Never log the API key.** Redact in all log lines.
11. **Capability/pain framing is downstream (Wave 2).** The adapter
    stores raw; the v8 prompt teaches the LLM the framing.
12. **Don'ts.** No transcripts. No region/language guessing from
    free-form location. No comment pagination / reply expansion. No
    retrying a quota wall. No storing parsed/normalized bodies.

**Divergences this skill documents** (so future sessions don't "fix"
them): from the source-adapter umbrella — quota-aware retry (not
retry-all), three-step fetch, two Bronze entity kinds under one source;
from `hackernews-source` — YouTube *has* retry, *no* token decomposition,
*needs* a key, *enriches and harvests comments*; from `reddit-source` —
per-instance limiter.

## 15. Tests

Mirror the HN test structure. `httpx.MockTransport`, no VCR.

**`tests/unit/llm/test_schemas.py` additions** — `YouTubeQuerySpec`
validation (`intent` Literal, `query`/`rationale` min_length, frozen);
`JobPlan.youtube_queries` permissive default; the generalized
`_attach_extra_source_queries` preserves BOTH `hn_queries` and
`youtube_queries` past a simulated locked-tail rebuild.

**`tests/unit/sources/test_youtube.py`** — pure helpers + adapter:

- `TestBuildUrls` — search URL (params, `publishedAfter` present vs
  omitted, key appended), videos URL (csv ids, `part=snippet,statistics`),
  comments URL.
- `TestRawRecordHelpers` — `video_to_raw_record` (`external_id`,
  `source`, verbatim body), `comment_to_raw_record` (carries
  `snippet.videoId`), `search_hit_to_raw_record`, `extract_video_ids`,
  `viewcount_of` (string → int, missing → 0).
- `TestYouTubeSourceFetch` (MockTransport):
  - no key → returns `[]`, no HTTP calls.
  - happy path → search + enrich + comments produce video and comment
    records with the right `external_id`/`source`.
  - partial success: one search query 500s → returns the others' records.
  - quotaExceeded on search query 2 → queries 3+ NOT attempted; records
    from query 1 still returned.
  - enrichment quotaExceeded → search-hit fallback records stored;
    comment step skipped.
  - commentsDisabled on a video → that video skipped, others harvested.
  - transient 5xx is retried (succeeds on retry); quotaExceeded is NOT
    retried (one attempt).
  - all searches fail → raises the first error.
- `TestYouTubeSourceLogging` — per-call log line carries the redacted
  URL, status, elapsed_ms, counts; the key never appears in clear.

**`tests/unit/orchestrator/test_youtube.py`**:

- `enqueue_youtube_task_for_job` idempotent on `(job_id, content_hash)`.
- reads `job.job_plan["youtube_queries"]` when present → `_compile_yt_queries`.
- template fallback when `job_plan` null OR fails validation; empty
  `youtube_queries` is graceful sparsity (enqueues, no template).
- `_compile_yt_queries` dedups on lowercased normalized query, caps at
  `MAX_YT_QUERIES=10` preserving LLM order, applies `published_after`.
- `_time_window_rfc3339` maps each window correctly; `all` → `None`;
  output is a valid `...Z` RFC 3339 string anchored at `as_of`.

**`tests/unit/workers/test_registry.py`** — `build_default_registry()`
includes a `"youtube"` adapter; it is constructed with `api_key=None`
when the setting is unset; `aclose_registry` closes it.

**`tests/unit/cli/test_run_parallel.py`** — extend to three branches:
stub all three adapters with detectable delays; assert all three run
concurrently and all three tasks flip to `done`; a failing one source
does NOT prevent the others from succeeding.

## 16. Documented divergences (so they're not "fixed" later)

- **From the source-adapter umbrella** (`.claude/skills/source-adapter/SKILL.md`):
  - "Retry with backoff, max 3" — YouTube DOES retry transient/rate-limit
    errors but treats `quotaExceeded`/`dailyLimitExceeded` as terminal
    (no retry). Documented in `youtube-source` item 7.
  - "One fetch returns the response verbatim" — YouTube does a *three-step*
    fetch (search → enrich → comments) and stores TWO entity kinds.
    Documented in `youtube-source` items 3, 8.
- **From the `hackernews-source` skill**: HN does no retry; YouTube does.
  HN decomposes to token-AND; YouTube is full-text (no decomposition).
  HN needs no key; YouTube needs one (no-ops without). HN stores search
  hits verbatim only; YouTube enriches with stats and harvests comments.
  Documented in `youtube-source` items 2, 4, 7, 8.
- **From the `reddit-source` skill**: Reddit uses a process-wide
  singleton limiter (two consumers share a budget); YouTube uses a
  per-instance limiter (one consumer). Documented in `youtube-source`
  item 9.
- **From the brainstorming default location**: spec goes to `docs/specs/`
  (project convention), not `docs/superpowers/specs/`.

## 17. Risks + invariants for future sessions to preserve

- **The locked Wave-0 Reddit tail must stay byte-for-byte Reddit-only.**
  Any new non-Reddit source field MUST be threaded through
  `_attach_extra_source_queries` (capture once, reattach once). The
  carry-through is fragile by design (§6).
- **Single-worker assumption is NOT lifted.** Three-way parallel works
  because the CLI routes around `claim_one` via `claim_known_task`.
  `claim_one` is untouched.
- **Prompt `VERSION` v7→v8 invalidates the combined Wave-0 cache for all
  jobs.** First runs after the bump are cold — expected.
- **Idempotency trap (unchanged).** Re-running the same
  `industry+location+as_of+time_window` returns the cached old job and
  short-circuits `plan_job`. To re-test the v8 prompt against the real
  LLM, change `--industry` or `--as-of`.
- **Quota is the operational ceiling.** ~9 jobs/day on the default
  10,000 units. Hitting the wall mid-job degrades cleanly (partial stop),
  never errors hard or retries into the wall. If 9/day is tight, request
  a Google quota increase or use a second project/key — no code change
  needed.
- **YouTube sparsity is graceful.** Empty/thin `youtube_queries` →
  no-op or small task; an unset key → empty result; a task with zero
  records completes `done`, not `failed`.
- **Two Bronze entity kinds under one source.** Wave 2 must route on the
  resource `kind` field (`youtube#video` vs `youtube#commentThread`, plus
  the rare `youtube#searchResult` fallback). Documented for the Wave 2
  slice.
- **Comment availability is not guaranteed.** `commentsDisabled` videos
  yield zero comment rows for that video — expected, not an error.

## 18. Decision record

- **Strategy:** YouTube as the third source (owner's call).
- **Fan-out behavior:** all three sources every run, no flags.
- **LLM contract:** Approach A — LLM emits ranked `YouTubeQuerySpec`
  candidates (query + intent + rationale); Python owns all mechanics.
- **Query cap:** `MAX_YT_QUERIES = 10` (owner-chosen; ~9 jobs/day quota).
- **Comment harvest:** `COMMENT_TOP_K = 50` videos by view count
  (owner-chosen).
- **Enrichment:** YES — `videos.list` for statistics, stored verbatim;
  view count is the demand signal and Wave 2 cannot fetch it later.
- **Intent role:** generation balance + downstream tag only; does NOT
  route API params (`order=relevance` for all).
- **Carry-through:** generalized to one `_attach_extra_source_queries`
  helper carrying all non-Reddit fields.
- **Retry:** quota-aware — retry transient/rate-limit, hard-stop on
  quotaExceeded.
- **Config:** dedicated `youtube_api_key` (not reusing `google_api_key`).
- **Limiter:** per-instance `AsyncLimiter(5, 1)`.
- **Skill creation:** `.claude/skills/youtube-source/SKILL.md`
  explicitly authorized by the owner.
- **Spec location:** `docs/specs/` (project convention).
- **Parallel execution:** third branch in `cli/run.py`'s `asyncio.gather`
  via the existing `claim_known_task`. Single-worker assumption preserved.

## 19. Sources

- [YouTube Data API v3 — Getting started / quota basics](https://developers.google.com/youtube/v3/getting-started)
- [Quota cost table ("Quota Calculator")](https://developers.google.com/youtube/v3/determine_quota_cost)
- [`search.list` reference](https://developers.google.com/youtube/v3/docs/search/list)
- [`videos.list` reference](https://developers.google.com/youtube/v3/docs/videos/list)
- [`commentThreads.list` reference](https://developers.google.com/youtube/v3/docs/commentThreads/list)
- [Errors reference (quota/rate reasons)](https://developers.google.com/youtube/v3/docs/errors)
- [API key vs OAuth](https://developers.google.com/youtube/registering_an_application)
- Companion design + skills: `docs/specs/2026-05-20-hackernews-source-design.md`,
  `.claude/skills/source-adapter/SKILL.md`,
  `.claude/skills/hackernews-source/SKILL.md`.

---

End of spec.
