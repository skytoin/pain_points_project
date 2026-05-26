# YouTube Source Adapter -- Operational Playbook

This file is the project's policy on YouTube. Read it end-to-end before
writing or modifying `src/discovery/sources/youtube.py`,
`src/discovery/orchestrator/youtube.py`, or planning YouTube queries
from a `JobPlan`. The numbered items below are cross-referenced by
number in commits and reviews -- don't renumber them.

The `source-adapter` skill is the umbrella contract (async, rate-limited,
retried, Pydantic-validated, idempotent, stored verbatim). This file is
the YouTube-specific layer on top -- and where YouTube deliberately
diverges, it says so.

The companion design doc is
`docs/specs/2026-05-22-youtube-source-design.md`.

---

## 1. Use the Data API v3 with an API key. No OAuth.

The YouTube Data API v3 supports read-only public data with a simple API
key -- no OAuth, no service account, no user token. Key lives in
`settings.youtube_api_key` (`SecretStr | None`). When the setting is
`None`, `YouTubeSource.fetch` returns `[]` immediately with a warning log
and makes zero HTTP calls. This is the "no-op without key" contract: the
task completes `done` with zero records, not `failed`.

Key is never logged in clear (see item 10). Never use `google_api_key`
for YouTube -- dedicate `youtube_api_key` so the two quotas stay
independent.

## 2. Quota is the harshest constraint. Budget around it.

The default daily quota is 10,000 units. Costs:

- `search.list` = **100 units** per call (the expensive one)
- `videos.list` = 1 unit per call
- `commentThreads.list` = 1 unit per call

A single job with 10 search queries + one enrichment batch + 50 comment
calls costs approximately 10*100 + 1 + 50 = ~1,051 units. This gives
roughly 9 full jobs per day on the default quota.

`MAX_YT_QUERIES = 10` (in `orchestrator/youtube.py`) is the hard cap on
search queries per task. This is owner-chosen to fit the quota budget.
Do not raise it without re-checking the per-job unit count.

## 3. Three-step fetch: search -> enrich -> harvest comments.

`YouTubeSource.fetch` runs three sequential steps:

1. **`search.list`** (100 units each): one call per compiled query.
   Returns up to 50 video ids. Stops early on `quotaExceeded`; partial
   success across queries otherwise.

2. **`videos.list` enrichment** (1 unit per batch of up to 50 ids):
   fetches snippet + statistics (including `viewCount`). View count is
   the demand signal; Wave 2 cannot fetch it later, so it is stored in
   Bronze verbatim. On `quotaExceeded`, stores un-enriched search hits
   as `kind=youtube#searchResult` and skips the comment step.

3. **`commentThreads.list`** (1 unit per video): harvested for the top
   `COMMENT_TOP_K = 50` videos ranked by view count. `commentsDisabled`
   on a video is a per-video skip (not an error); `quotaExceeded` stops
   the loop early but keeps already-harvested comments. Each returned
   thread passes through the **comment-quality floor** before storage.

The three helpers `_search_all`, `_enrich_videos`, `_harvest_comments`
each stay under 60 lines. `fetch` itself is a thin 7-line orchestrator.

### Comment-quality floor (`keep_comment`)

`_harvest_comments` drops low-information comment threads BEFORE storing,
the comment-level analog of Reddit's `keep_post()` (a cheap deterministic
fetch-floor, NOT a violation of "Bronze stores raw" -- Bronze stores raw
*survivors*). Rule:

> keep if the top-level comment has at least one letter (any script, via
> Unicode `str.isalpha()`) AND (`len(text) >= MIN_COMMENT_CHARS` OR
> `likeCount >= MIN_COMMENT_LIKES`).

Constants: `MIN_COMMENT_CHARS = 45`, `MIN_COMMENT_LIKES = 7`. This drops
emoji/symbol-only reactions and short low-engagement one-liners
("WOW", "nice video"), KEEPS non-English text, and rescues short-but-
upvoted complaints. On the first IoT run it kept ~2,507 of 4,536 threads.
It does NOT judge topical relevance or whether a comment is a real pain
point -- that is semantic and stays Wave 2's job (a long off-topic
comment still passes). Text is read from
`snippet.topLevelComment.snippet.textOriginal` (fallback `textDisplay`);
likes from `.likeCount`. Per-video `kept`/`dropped` counts are logged.

## 4. YouTube is full-text, not token-AND. No decomposition.

This is the single most important difference from the HackerNews adapter.
YouTube's search engine is a full-text relevance system -- it does NOT
require all tokens to co-occur (unlike Algolia HN). Long, natural-
language, emotion-shaped phrases ("why I quit commercial cleaning") work
well and often better than short phrases.

Consequence: no `decompose_keyword` call, no token truncation. The
query from `YouTubeQuerySpec.query` is passed through near-verbatim
after whitespace normalization. The `|` (OR) and `-` (NOT) operators are
not supported; do not try to use them.

## 5. Comments are the best pain surface. The seven surfaces.

YouTube is a pain surface, not just a video library. The richest signal is
usually NOT the video itself but the **comments** under it. The seven
surfaces where pain hides on YouTube:

1. Comments under tutorials -- literal "I followed this exactly but Y
   breaks" pain, the single best surface.
2. "Why I quit [X]" videos -- pure pain monologues.
3. Review videos and their comments -- explicit cons the reviewer missed.
4. "Day in the life of [profession]" -- visible friction, manual
   workarounds, the five apps open at once.
5. "Things I wish I knew before [becoming X]" -- retrospective pain.
6. Storytime / horror-story videos -- pain compiled across many people.
7. Live Q&A / AMAs -- pain verbalized in real time.

The Wave 0 v8 prompt teaches the LLM to generate queries targeting these
surfaces for the specific industry. The adapter stores both video and
comment resources; Wave 2 routes on the `kind` field.

## 6. order=relevance always. publishedAfter from the time window.

Every search query uses `order=relevance` regardless of `intent`. This is
the highest-precision choice for a scarce 10-query budget -- relevance
ranking returns the most-watched pain-expressing content first.

`publishedAfter` is set from `JobSpec.time_window` via
`_time_window_rfc3339` in `orchestrator/youtube.py`. It is an RFC 3339
`...Z` string anchored at `as_of` midnight UTC. `time_window="all"`
omits the filter entirely (no `publishedAfter` param).

`intent` (`"complaint"` or `"discussion"`) does NOT route to any API
parameter. It is used by the v8 prompt to balance query generation
(complaint queries surface pain-monologue videos; discussion queries
surface tutorials/reviews where comments are the real signal) and will
be used as a downstream tag in Wave 2. Do not add intent-based routing
to API params -- this is a deliberate non-goal (spec §2, §8).

## 7. Quota-aware retry: retry transient, STOP on quota.

The retry logic is hand-rolled in `YouTubeSource._get_json` with an
injectable `sleep` callable (same pattern as `RedditSource`, NOT
tenacity). Max 3 retries by default.

Classification rules (inside `_classify`):

- `403 quotaExceeded` / `403 dailyLimitExceeded` -- **HARD STOP, no
  retry**. Raise `YouTubeQuotaExceeded` immediately. Never retry into
  the wall; the quota resets at midnight Pacific.
- `403 commentsDisabled` -- raise `CommentsDisabled` (per-video skip in
  `_harvest_comments`; NOT a retryable error at the `_get_json` level).
- `403 rateLimitExceeded` / `403 userRateLimitExceeded` -- transient;
  retry with exponential backoff (5s, 10s, 20s, capped at 300s).
- `429` -- treated as a rate-limit; retry with backoff.
- `5xx` (500-599) -- transient; retry with backoff.
- Other `4xx` -- non-retryable; `raise_for_status()` propagates.

The `sleep` is injectable so tests can pass `_noop_sleep` for speed. The
actual sleep values are `min(5.0 * (2.0 ** attempt), 300.0)`.

## 8. Bronze stores raw. Two entity kinds under source="youtube".

`YouTubeSource` stores three possible resource kinds, all verbatim:

- `youtube#video` -- from `videos.list` enrichment (the normal path).
  Carries `snippet` + `statistics` (including `viewCount`).
- `youtube#commentThread` -- from `commentThreads.list`. Carries
  `snippet.videoId` so the parent video link survives.
- `youtube#searchResult` -- fallback only, when enrichment hits
  `quotaExceeded` before it can run. No statistics. This is rare.

All three use `source="youtube"` in `raw_records`. Wave 2 MUST route on
the `kind` field inside `body` to distinguish them. Do not parse,
normalize, or trim bodies in the adapter -- Bronze is verbatim.

`external_id` conventions:
- Video records: the YouTube video id (e.g. `"dQw4w9WgXcQ"`).
- Comment records: the commentThread id.
- Search fallback records: the video id from `id.videoId`.

## 9. Per-instance limiter, not a singleton.

Each `YouTubeSource` instance creates its own `AsyncLimiter(5, 1)` (5
requests/second polite ceiling). This is a per-instance limiter because
YouTube has exactly ONE consumer in this pipeline (Wave 1 fetch -- there
is no YouTube sub-search analog like the Reddit subreddit-discovery step).
There is nothing for a process-wide singleton to coordinate.

Do not add a `youtube_ratelimit.py` singleton. Do not "fix" this to
mirror Reddit's singleton (item 2 of the `reddit-source` skill). The
`AsyncLimiter` is injectable via the constructor for test speed.

## 10. Never log the API key.

All log calls go through `_log_call`, which passes the URL through
`_redact_key` before logging. `_redact_key` replaces the `key=...` query
parameter value with `REDACTED`.

The actual key never appears in any `loguru` output. This is tested in
`tests/unit/sources/test_youtube.py::TestLogging`. If you add new log
lines that include the URL, run them through `_redact_key` first.

## 11. Capability/pain framing is Wave 2, not the adapter.

The adapter stores raw resources. Whether a video is "pain" vs
"capability", how to extract tools mentioned in comments, whether a
"day in the life" video reveals a workflow gap -- all of that is Wave 2
(pain classification LLM station) concern.

The v8 prompt teaches the LLM to generate queries targeting pain surfaces
for the requested industry. The `intent` tag (`complaint` / `discussion`)
captures the framing at query-generation time and will flow through to
Wave 2. The adapter itself is blind to intent at fetch time.

## 12. Don'ts.

- **No transcripts/captions.** Captions require a separate API surface
  (often OAuth-gated, quota-heavier). Deferred to a future slice.
- **No `regionCode`/`relevanceLanguage` from free-form location.**
  `JobSpec.location` ("NY", "US", "the Midwest") does not map cleanly
  to ISO codes. A bad mapping silently narrows results. Both params
  are omitted.
- **No comment pagination / reply expansion.** One
  `commentThreads.list` call per video (up to 100 top-level comments, 1
  unit). No `pageToken` walking, no `comments.list` reply fetching.
- **No retrying a quota wall.** `quotaExceeded` / `dailyLimitExceeded`
  are terminal. Stop cleanly and return partial results. The quota
  resets at midnight Pacific; retrying before then wastes units on
  calls that will fail again.
- **No storing parsed bodies.** Bronze is verbatim. The `body` field in
  `RawRecord` receives the full API response dict unchanged.
- **No `order` routing on `intent`.** `order=relevance` for everything.
  Do not add intent-based routing by stealth.
- **No process-wide singleton limiter.** Per-instance only (item 9).

---

## Divergences from related skills (single point of truth)

### From the `source-adapter` umbrella

- **Quota-aware retry, not retry-all** (item 7). The umbrella says
  "retry with backoff, max 3 attempts" uniformly. YouTube deliberately
  treats `quotaExceeded`/`dailyLimitExceeded` as terminal with zero
  retries. This is load-bearing: retrying into the quota wall wastes
  the remaining units and delays the stop.
- **Three-step fetch, two Bronze entity kinds** (items 3, 8). The
  umbrella's "one fetch returns the response verbatim" contract is
  extended: YouTube's fetch is a three-step orchestration (search ->
  enrich -> comments) and produces two distinct entity kinds under one
  `source` value. Wave 2 routes on `kind`.
- **Retry is hand-rolled with injectable sleep, NOT tenacity.** The
  umbrella mentions tenacity as the retry library. YouTube (like Reddit)
  uses a hand-rolled loop with injectable `sleep` for test-speed control.

### From the `hackernews-source` skill

- **YouTube has retry; HN does not.** HN's "no retry" decision was
  emphatic and HN-specific. YouTube faces transient 5xx and rate-limit
  errors in practice -- retry is needed.
- **No token decomposition** (item 4). HN is strict token-AND and
  requires decomposition to 2 tokens. YouTube is full-text relevance;
  long natural-language phrases work better than short phrases.
- **YouTube requires an API key; HN does not.** HN (Algolia) is
  anonymous. YouTube needs a valid `YOUTUBE_API_KEY` or returns nothing.
- **YouTube enriches with stats and harvests comments.** HN stores
  search hits verbatim only. YouTube does a three-step fetch.

### From the `reddit-source` skill

- **Per-instance limiter** (item 9). Reddit uses the `reddit_ratelimit`
  process-wide singleton because Wave 0 sub-search and Wave 1 fetch
  share ONE 10-req/min budget. YouTube has one consumer; a singleton
  would add complexity with no coordination benefit.

All divergences are user-approved, surfaced in the spec
(`docs/specs/2026-05-22-youtube-source-design.md` §16, §18), and
documented inline above so future sessions don't "fix" them.
