# HackerNews source adapter — design

**Date:** 2026-05-20
**Status:** approved (brainstorm phase) — pending spec review + user sign-off
**Author:** Claude Opus 4.7 (1M context), in session with the project owner

---

## 1. Goal

Add a second Wave-1 source to the discovery pipeline: Hacker News, via
the Algolia HN Search API. After this slice ships, every `discovery run`
fans out to **both Reddit and HN concurrently** in a single job, so the
wall-clock time of a job is `max(reddit_time, hn_time)`, not the sum.
Wave 0 (the existing OpenAI gpt-5.4 query expansion station) is taught
to emit a third output, `hn_queries`, alongside the existing
`reddit_queries` and `reddit_subreddits`. Bronze (`raw_records`) gains
HN rows with `source="hackernews"`, stored verbatim. Nothing about how
Reddit works changes — the Reddit code path is untouched.

## 2. Non-goals

- **Wave 2 (pain classification) is out of scope.** Bronze HN rows stay
  unclassified until Wave 2 lands as its own slice (documented #1
  next slice; see handoff). The HN guide's downstream concerns
  ("capability not pain" tagging, snippet construction, permalink
  fallback, missing-`url` handling) are explicitly **deferred to Wave 2**
  because this project's locked contract is *Bronze stores raw, Wave 2
  parses*.
- **No DRY extraction of Reddit's retry policy.** The session setup
  flagged this as a candidate ("3rd consumer rule"), but the HN guide
  explicitly says HN should not carry Reddit's retry dance. HN is no
  longer a clean third consumer; its needs are a strict, smaller
  subset. Defer any DRY extraction to a future slice; it would be its
  own brainstorm.
- **No subreddit-discovery analog for HN.** HN is a flat site. The
  closest structural thing is Algolia tags (`story`, `show_hn`, …),
  picked deterministically in Python from the LLM's per-candidate
  `intent` flag. The LLM emits NO HN field analogous to
  `reddit_subreddits`. `hn_queries` is the only new HN field on
  `JobPlan`.
- **No multi-worker concurrency lift.** CLAUDE.md's single-worker
  assumption stays. Per-job parallelism between Reddit and HN is
  achieved by directly dispatching the two known task ids in
  `cli/run.py`, routing around `claim_one` (which is documented
  single-worker-safe only). If a future slice introduces multi-job
  concurrency, lifting that assumption — the worker comment already
  sketches the SQL — is its own slice.
- **No CLI flag work.** Both sources run every job; no `--only`, no
  `--with-hn`. Per `discovery run` clarification: "Both, every run".

## 3. Background (one paragraph each)

**Why HN now.** The owner picked HN over Wave 2 after the honest
tradeoff was surfaced (Wave 2 has higher signal-quality payoff but the
owner held course on HN to broaden inputs).

**What's locked from prior slices we must respect.**

- The Wave-0 station's *deterministic tail is UNCHANGED and order-
  preserved*. Four `JobPlan.model_construct(reddit_queries=...,
  reddit_subreddits=...)` rebuilds inside the station MUST stay
  Reddit-only. (See §6 for how we carry `hn_queries` across this tail.)
- `JobPlan.reddit_queries` band stays 25–30 (superseded the prior 10–15).
- `MIN_VALID_QUERIES=10` (decoupled Reddit floor) stays.
- Single-worker assumption — `claim_one` uses SELECT-then-UPDATE, race-
  unsafe under concurrent callers — stays.
- `raw_records.body` stores raw API responses verbatim; Wave 2 parses.
- Wave 0 brainstorms; Python validates. The LLM/Python split:
  creativity in the prompt, exactness in tested code.
- `(source, external_id)` is UNIQUE on `raw_records`. Verbatim HN hits
  dedup by `objectID`.
- `JobPlan` is `extra="allow"`, but app code must *not* read from
  `model_extra`; add a typed field and read from that.

**The Reddit/HN asymmetry.** Reddit rewards pain/frustration/workflow-
gap angles ("frustrated with X", "wish there was Y"). HN rewards
capability/launch/technical-debate angles ("Show HN: X for Y", "X in
Rust", "why we chose X over Y"). These are not "the same queries, two
formats" — they are genuinely different. The v6 prompt must teach the
LLM HN's *own* construction principles, not a pain-phrase port.

## 4. HN Algolia API — facts the design depends on

- **Base URLs.** `https://hn.algolia.com/api/v1/search` (relevance-
  sorted) and `https://hn.algolia.com/api/v1/search_by_date` (newest-
  first). No auth, no User-Agent requirement, generous rate limit
  (treated as effectively unlimited for a scanner).
- **Parameters.** `query` (full-text), `tags`, `numericFilters`,
  `hitsPerPage` (max 1000), `page` (zero-indexed). We will not paginate.
- **`tags` syntax.** Comma = AND, parenthesized list = OR. Example:
  `tags=story,author_pg` (AND), `tags=(story,show_hn)` (OR). Values:
  `story`, `comment`, `ask_hn`, `show_hn`, `poll`, `front_page`,
  `author_{username}`, `story_{id}`.
- **`numericFilters` syntax.** Operators `<, <=, =, !=, >=, >`. Comma
  = AND. Filterable fields include `created_at_i` (unix seconds),
  `points`, `num_comments`. Example:
  `created_at_i>1715040000,points>5,num_comments>3`.
- **Strict token-AND on `query`.** No `OR` operator. Every content token
  must co-occur in the result. Long phrases starve the source —
  decompose to ≤2 content tokens before querying.
- **Response shape.** `{ hits: [...], nbHits, page, nbPages, ... }`.
  Each hit: `objectID` (always present, our `external_id`), `title`,
  `url` (may be null for Ask/Show HN text posts — Wave 2 handles
  permalink fallback), `points`, `num_comments`, `author`,
  `created_at` (ISO), often `_tags`, and `story_text`/`comment_text`
  on text posts.

## 5. Architecture overview

```
                       discovery run
                             │
                             ▼
                       create_job(spec)
                             │
                             ▼
                   plan_job (Wave 0, inline)
                             │
              run_query_expansion(spec) → JobPlan
              ├── existing grounded Reddit chain (UNCHANGED tail)
              │     · phrase gen → /subreddits/search → middle
              │     · select+design → ground → finalize
              └── NEW: hn_queries carried across the Reddit tail in
                    one place (capture once, reattach once)
                             │
                       Job.job_plan ← plan.model_dump()
                             │
            ┌────────────────┴─────────────────┐
            ▼                                  ▼
  enqueue_reddit_task_for_job        enqueue_hn_task_for_job
            │                                  │
            └─────── asyncio.gather ───────────┘   ← parallel dispatch
                             │                          (cli/run.py;
                             ▼                          routes around
                  RedditSource │ HackerNewsSource       claim_one — see §11)
                             │
                             ▼
                       raw_records
                  (source ∈ {reddit, hackernews})
```

Wave 0 failure path is unchanged: `QueryExpansionError` → `job.job_plan`
stays null → both orchestrators detect null and fall back to their
deterministic no-LLM templates. Reddit's template is the existing one;
HN gets a small new one. Both sources still run; both still produce
Bronze rows.

## 6. Wave-0 station — the carry-through fix (the locked-tail finding)

**Finding.** The deterministic tail in
`src/discovery/llm/stations/query_expansion.py` rebuilds the plan with
`JobPlan.model_construct(reddit_queries=..., reddit_subreddits=...)`
at four sites in the locked tail: `_ground_selection`,
`_force_time_window`, `_merge_baseline_subreddits`, and
`_drop_invalid_queries` (`_finalize` orchestrates these helpers in
order but does not itself call `model_construct`). `model_construct` keeps *only the fields passed*.
Adding `hn_queries` to `JobPlan` would cause every one of those
reconstructions to silently delete the HN queries as the plan flows
through the tail. The tail is locked ("UNCHANGED and order-preserved"
— prior decision).

**Fix.** Don't touch the locked tail helpers. Capture `hn_queries` once in
`run_query_expansion` immediately after the LLM call (`_select_and_design`
returns a `JobPlan` whose `hn_queries` field came directly from the
v6 LLM output), run the locked Reddit tail untouched, then reattach
`hn_queries` once to the final plan via a single `model_construct`
before caching. Mechanically:

```python
async def run_query_expansion(spec: JobSpec) -> JobPlan:
    key = cache_key(...)
    cached = get_cached(_cache, key, JobPlan)
    if cached is not None:
        return cached

    phrases = await _generate_phrases(spec)
    candidates = await _discover_subreddits(phrases)
    raw_plan = await _select_and_design(spec, candidates)

    hn_queries = list(raw_plan.hn_queries)            # NEW: capture once
    grounded = _ground_selection(raw_plan, candidates)
    final_plan = _finalize(grounded, spec)
    final_plan = _attach_hn_queries(final_plan, hn_queries)  # NEW
    put_cached(_cache, key, final_plan)
    return final_plan


def _attach_hn_queries(
    plan: JobPlan, hn_queries: list[HackerNewsKeywordSpec]
) -> JobPlan:
    """Single point that re-attaches hn_queries to the post-tail plan.
    The locked Reddit tail rebuilds the plan via model_construct with
    only Reddit fields — we put hn_queries back here, also via
    model_construct so we skip re-running validation on the (already-
    pruned) Reddit fields.
    """
    return JobPlan.model_construct(
        reddit_queries=plan.reddit_queries,
        reddit_subreddits=plan.reddit_subreddits,
        hn_queries=hn_queries,
    )
```

**Invariant for future sessions.** If anyone changes the locked tail's
reconstruction pattern in the future, they MUST preserve `hn_queries`
through it. The carry-through helper exists *because* the tail loses
non-Reddit fields by design.

## 7. Schema additions (`src/discovery/llm/schemas.py`)

```python
class HackerNewsKeywordSpec(BaseModel):
    """Wave 0 LLM HN keyword candidate. Python downstream decomposes,
    routes by intent, and compiles to an Algolia URL — see §10.
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
            "launch → fired against /search_by_date with tags=show_hn "
            "and a relaxed quality floor (recency is the signal). "
            "context → fired against /search with tags=story and the "
            "standard points/num_comments floor."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this HN candidate is worth running.",
    )


class JobPlan(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    reddit_queries: list[RedditQuerySpec] = Field(min_length=25, max_length=30)
    reddit_subreddits: list[str] = Field(default_factory=list)
    hn_queries: list[HackerNewsKeywordSpec] = Field(default_factory=list)  # NEW
```

**Permissive default (no `min_length`) is deliberate.** A strict floor
on `hn_queries` could let HN under-production raise
`QueryExpansionError` and sink the Reddit grounded plan with it. The
HN guide and the owner's fan-out decision say HN sparsity must
*degrade gracefully*. If `hn_queries` arrives empty, the HN
orchestrator falls back to its template (§10). Mirrors
`reddit_subreddits`'s permissive default.

## 8. v6 prompt — adding HN keyword candidates to the existing Wave-0 prompt

`src/discovery/llm/prompts/query_expansion.py` bumps `VERSION` from
`"v5"` to `"v6"`. Bumping correctly invalidates the combined Wave-0
cache (keyed by `f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}"`
— see the station). The existing "Two kinds of queries" section
(Reddit-internal kinds 1+2) is untouched; we add a new top-level Kind 3
section after it, and update the "What to emit" master section to
mention three fields instead of two. The `build_user_message`
function gets a one-line nudge.

**The exact prompt addition (the section the LLM will see):**

```
# Kind 3 — Hacker News keyword candidates (a SEPARATE output: hn_queries)

Hacker News is a flat site — NO communities, NO subreddit equivalent.
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

1. SHORT, DENSE PHRASES — 2 to 4 words. Python will strip filler
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
   spot — BUT put the distinctive word in the first two positions
   so decomposition keeps it. Examples (every distinctive token
   survives): "local-first CRM", "Rust vector-db", "TypeScript
   agents", "scheduling CLI", "billing CRDT". Bury "CRM" or
   "framework" or "database" at position 3 and Python silently
   drops the very word that makes the phrase HN-suitable.

5. NO Reddit-flavored pain phrasings. "I would pay", "frustrated
   with", "wish there was", "tired of" — these all return zero or
   near-zero on HN. They live in `reddit_queries`, not `hn_queries`.

6. DO NOT spend content tokens on tag-redundant words. Don't write
   "Show HN", "HN", "Ask HN" inside the keyword — `intent=launch`
   already routes to `tags=show_hn` and `intent=context` to
   `tags=story` server-side. Putting those words in the keyword
   burns both content slots on the tag filter (the LLM's most
   common HN failure mode). Spend both content tokens on the
   substantive industry/capability terms.

## Tag each candidate's INTENT — launch or context

For every HN candidate you emit, mark `intent`:

- launch — phrase shaped to match a fresh "Show HN" launch (product
  name shape, "X for Y", new-thing framing). Python fires these
  against the date-sorted endpoint with relaxed quality filters so
  brand-new launches with low points still surface.
- context — phrase shaped to match technical-discussion stories
  (debates, comparisons, deep-dives). Python fires these against
  the relevance-sorted endpoint with a server-side karma + comments
  floor.

AIM FOR ROUGHLY TWO-THIRDS LAUNCH AND ONE-THIRD CONTEXT (e.g. 6
launch + 3 context, or 8 launch + 4 context). The rationale tag
drives the routing per candidate; the 2:1 ratio is a target, not a
quota — Python does NOT enforce the ratio, it routes each candidate
strictly by its own `intent` tag.

## What to emit for HN

Emit 8-15 `HackerNewsKeywordSpec` objects in `hn_queries` — BUT if
the industry has weak HN coverage (trades, local services, non-
technical verticals), emit FEWER or ZERO candidates rather than
inventing tech-framed phrases. Quality over quota; downstream is
fine with an empty list. Each candidate has:

- `keyword`   — the raw phrase, 2-4 words, casing preserved.
- `intent`    — `launch` or `context`.
- `rationale` — one short sentence: what HN content this should
                surface and why it's HN-suitable.

EMIT YOUR STRONGEST CANDIDATES FIRST. Python caps the fired set at
6 in your emitted order, so ordering is a ranking signal — your
best candidates must appear in the first ~6 positions.

Python downstream will decompose each keyword (drop stopwords, keep
≤2 content tokens, preserve casing), dedupe, route by `intent`,
build server-side `numericFilters` from the job's time window
(relaxed for launch queries), and cap the total at ~6 actually fired
against the API. Emit MORE than 6 candidates so the post-decomposition
survivors still cover both intents.

## HN illustration — ONE example industry only (do NOT reuse these)

For the example industry "personal CRM for solo founders" (an HN-
native vertical chosen because it shows the pattern cleanly). Note
how every example puts the distinctive token in the FIRST TWO
positions so decomposition keeps it:

- "local-first CRM" (launch) — local-first sub-trend launches.
- "CRM CLI" (launch) — terminal-first product launches.
- "OSS CRM" (launch) — open-source CRM launches.
- "SQLite CRM" (launch) — SQLite-backed launch pattern.
- "CRM founder" (context) — discussion of how founders organize
  relationship work.
- "contact privacy" (context) — privacy-debate angle on contact
  storage.

For ANY OTHER industry you must RE-DERIVE different industry-specific
HN-shaped angles. Do not bolt this CRM vocabulary onto another
industry the way you must not reuse the wedding-photography
illustration above.
```

The master "What to emit" section in v5 is updated:

```
You will emit a JSON object validated as `JobPlan` with THREE fields:

- `reddit_queries`      — 25-30 RedditQuerySpec (see Kinds 1 & 2 above).
- `reddit_subreddits`   — your shortlist of domain-relevant subs.
- `hn_queries`          — 8-15 HackerNewsKeywordSpec (see Kind 3 above).
                          Re-derive HN-shaped angles for THIS industry;
                          do NOT translate the reddit_queries to HN.
```

And `build_user_message` adds one line near the closing instruction:

```
Plus 8-15 hn_queries: HackerNews keyword candidates re-derived for
THIS industry (capability/launch framing, NOT pain phrasing). Tag
intent per candidate; aim ~⅔ launch / ⅓ context.
```

**Why this prompt design respects Approach A.** The mechanical rules
(token-AND decomposition, the 2:1 routing, server-side
`numericFilters`, the time-window epoch) are NOT in the prompt — they
are in tested Python (§9, §10). What IS in the prompt is what the LLM
uniquely judges: which keywords are HN-suitable, in what shape, with
what intent. Creativity stays in the prompt; exactness stays in code.

## 9. Keyword tokens module (`src/discovery/sources/keyword_tokens.py`)

A small pure module — single responsibility, easily extractable later
by GitHub/arXiv adapters but NOT pre-generalized for them (YAGNI).

```python
"""Token decomposition for token-AND search APIs (HN Algolia).

Splits a raw keyword phrase into the small set of high-signal content
tokens HN's strict token-AND search will accept. Long phrases starve
the source, so we keep only the first 2 surviving tokens after a
small stopword strip, with original casing preserved (acronyms like
MCP, CLI, RAG, LLM matter on HN).

Reusable later by other token-AND backends (GitHub code search, arXiv,
etc.) — kept here in the HN-adopting slice without pre-generalization
for unbuilt sources.
"""

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the",
    "for", "with", "to", "of", "in", "on",
    "and", "or",
})

MAX_TOKENS: int = 2


def decompose_keyword(keyword: str) -> list[str]:
    """Return up to 2 content tokens from a raw HN keyword phrase.

    - Whitespace-split (no punctuation surgery — HN's tokenizer is
      simple; we feed it as-is once stopwords are gone).
    - Filter tokens whose LOWERCASED form is in the stopword set
      (so the comparison is case-insensitive but the surviving tokens
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

**Stopword set is deliberately tiny** per the HN guide ("a big stopword
list starts eating real content"). It is not exported as a knob; if
the list ever needs tuning, that's a code change with tests.

## 10. HN orchestrator (`src/discovery/orchestrator/hackernews.py`)

Mirrors `orchestrator/reddit.py`. Public surface:

- `enqueue_hn_task_for_job(session, job) -> Task` — idempotent on
  `content_hash`, mirrors `enqueue_reddit_task_for_job`.
- `hn_keyword_candidates_for_spec(spec) -> list[dict]` — the no-LLM
  template fallback. Used when `job.job_plan` is null OR contains a
  thin/empty `hn_queries`.

Internal helpers (each one job, ≤60 lines per CLAUDE.md):

- `_queries_from_job_plan(job) -> list[dict] | None` — validates the
  stored `JobPlan`, returns compiled fetch-params dicts; returns None
  (template fallback) when `job_plan` is null or fails validation.
- `_compile_hn_queries(specs, spec) -> list[dict]` — the deterministic
  pipeline: decompose each `keyword` to ≤2 tokens, drop empties, dedup
  on the joined token tuple (case-sensitive, so `MCP` ≠ `mcp`), route
  by `spec.intent`, build `numericFilters` from `spec.time_window` and
  `as_of`, cap the total at `MAX_HN_QUERIES=6` (preserving LLM order).
- `_routing_for(intent) -> tuple[str, str, list[str]]` — returns
  `(endpoint, tags, extra_numeric_filters)` for `launch` vs `context`.
- `_time_window_epoch(time_window, as_of) -> int | None` — the
  JobSpec time-window → unix-seconds floor. `all` → None (omit
  `created_at_i`).
- `_build_fetch_params(query_tokens, endpoint, tags, numeric_filters)
  -> dict` — the per-query dict shape that `HackerNewsSource.fetch`
  consumes.

**Routing table** (deterministic, NOT in the prompt). The `endpoint`
column shows the URL path for readability; the compiled fetch-params
dict below stores it as a bare transport flag (`"search"` /
`"search_by_date"`) which `build_search_url` prepends with the base URL
at fetch time.

| `intent`  | endpoint              | `tags` | `numericFilters` beyond `created_at_i` |
|-----------|-----------------------|--------|----------------------------------------|
| `launch`  | `/search_by_date`     | `show_hn` | none (relaxed — fresh launches legitimately have low points) |
| `context` | `/search`             | `story`   | `points>5,num_comments>3`              |

**Time-window → unix-seconds floor:**

| `time_window` | offset      |
|---------------|-------------|
| `hour`        | 3 600 s     |
| `day`         | 86 400 s    |
| `week`        | 604 800 s   |
| `month`       | 2 592 000 s (30 d) |
| `year`        | 31 536 000 s (365 d) |
| `all`         | omit `created_at_i` entirely |

Anchor: `as_of` at midnight UTC. `epoch = int((datetime.combine(as_of,
time.min, tzinfo=UTC) - timedelta(seconds=OFFSET)).timestamp())`.

**Compiled fetch-params dict shape** (consumed by
`HackerNewsSource.fetch`):

```python
{
    "endpoint": "search" | "search_by_date",  # transport flag; build_search_url prepends the base URL at fetch time
    "query": "Personal CRM",                  # space-separated tokens after decomposition
    "tags": "show_hn" | "story",
    "numeric_filters": "created_at_i>1715040000" | "created_at_i>...,points>5,num_comments>3",
    "hits_per_page": 30,
}
```

**Cap policy.** `MAX_HN_QUERIES = 6` (per HN guide item 7 — more
queries = more downstream LLM cost without proportional yield). The
compiler keeps the LLM's emitted order and truncates after the cap.

**Template fallback** (`hn_keyword_candidates_for_spec`):

A no-LLM fallback so HN works with `OPENAI_API_KEY` unset, exactly
like Reddit's template fallback. It emits a small, deterministic set
of candidates from the industry literal and a tiny capability-phrase
list:

```python
def hn_keyword_candidates_for_spec(spec: JobSpec) -> list[dict]:
    """Deterministic HN fallback — no LLM. Capability word FIRST so
    decomposition keeps it for multi-word industries (e.g.
    "commercial cleaning CLI" would drop "CLI"; "CLI commercial
    cleaning" keeps "CLI" + the first industry word). Same compile
    path as the LLM output."""
    industry = spec.industry
    return _compile_hn_queries(
        [
            HackerNewsKeywordSpec(
                keyword=f"CLI {industry}", intent="launch",
                rationale="(template) CLI launch fallback",
            ),
            HackerNewsKeywordSpec(
                keyword=f"OSS {industry}", intent="launch",
                rationale="(template) OSS launch fallback",
            ),
            HackerNewsKeywordSpec(
                keyword=f"API {industry}", intent="launch",
                rationale="(template) API launch fallback",
            ),
            HackerNewsKeywordSpec(
                keyword=f"workflow {industry}", intent="context",
                rationale="(template) workflow discussion fallback",
            ),
        ],
        spec,
    )
```

**Idempotency.** `enqueue_hn_task_for_job` uses
`hash_params({"source": "hackernews", "action": "fetch", "params":
params})` as `content_hash` and short-circuits on
`(job_id, content_hash)` collision — identical to Reddit.

## 11. HN source adapter (`src/discovery/sources/hackernews.py`)

```python
class HackerNewsSource(BaseSource):
    name = "hackernews"
    rate_limit = (5, 1)   # 5 req/s polite — Algolia's real ceiling is ~10k/hr
```

**Per-instance `AsyncLimiter`**, not a process-wide singleton. Only one
HN consumer exists (Wave-1 fetch — HN has no subreddit-discovery
analog), and Algolia's limit is effectively unlimited, so the shared-
budget reason that forced `reddit_ratelimit.py` doesn't apply.
Constructor signature mirrors `RedditSource`:

```python
def __init__(
    self,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
    timeout: float = 30.0,
) -> None:
    self._client = client if client is not None else httpx.AsyncClient(timeout=timeout)
    self._owned_client = client is None
    self._limiter = limiter if limiter is not None else AsyncLimiter(max_rate=5, time_period=1)
```

No `sleep` parameter (no retry, no backoff — see below). No `user_agent`
(Algolia doesn't require one).

**Pure helpers:**

- `build_search_url(query_params) -> str` — pure URL builder. Routes
  the `endpoint` transport flag (`"search"` vs `"search_by_date"`) to
  the right base URL; serializes `query`, `tags`, `numericFilters`,
  `hitsPerPage`. Always sets `page=0` (no pagination).
- `hit_to_raw_record(hit) -> RawRecord` —
  `source="hackernews"`, `external_id=str(hit["objectID"])`, `body=hit`
  (verbatim). No snippet, no permalink fallback, no body trimming —
  Wave 2 owns those.
- `keep_hit(hit) -> bool` — near-noop. Server-side `numericFilters`
  does the quality floor. Locally we only drop hits with no `objectID`
  (impossible per Algolia's docs but cheap defense), nothing else.

**`fetch(params) -> list[RawRecord]`** — the partial-success loop, no
retry:

```python
async def fetch(self, params: dict) -> list[RawRecord]:
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


async def _run_one(self, query: dict) -> list[RawRecord]:
    url = build_search_url(query)
    started_at = time.monotonic()
    async with self._limiter:
        response = await self._client.get(url)
    elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
    response.raise_for_status()   # non-2xx → HTTPStatusError, recorded above
    payload = response.json()
    hits = payload.get("hits", [])

    out: list[RawRecord] = []
    for hit in hits:
        if keep_hit(hit):
            out.append(hit_to_raw_record(hit))

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
```

**No retry, no backoff** — per the owner's "Minimal, HN-native" choice
and the HN guide's item 12 ("Don't over-engineer it the way Reddit
needs"). One GET per query. A non-2xx or `httpx.HTTPError` records the
query's error and moves on. The project-locked partial-success
contract overrides the guide's single-query "just throw" because we
batch ~6 queries per task: if some succeed, return what worked; only
when *all* fail does `_run_one` raise so the worker can record the
task as failed.

**`aclose()`** override closes the owned `httpx.AsyncClient` so the
test environment's `filterwarnings=["error"]` doesn't fail tests on
unclosed clients. `aclose_registry` already iterates the registry and
calls `aclose` on every adapter (commit ffc2abc).

**Per-query log line** (skill-21 analog): `url`, `status`,
`elapsed_ms`, `count_before_filter`, `count_after_filter`, `endpoint`,
`tags`. Same diagnostic shape as Reddit's, dropped one row at a time.

## 12. Parallel fan-out — the `cli/run.py` change

**Constraint.** `worker.py`'s `claim_one` is documented single-worker-
safe only: "Single-worker safe: the SELECT-then-UPDATE pair is wrapped
in one transaction. For multi-worker concurrency, switch to
UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING * (atomic in
SQLite) or FOR UPDATE SKIP LOCKED (Postgres)." Running two concurrent
`run_worker_once` calls would race: both could `SELECT` the same task,
both flip it to `running`, both increment `attempts`, the task gets
processed twice.

**Decision.** Keep `claim_one` as-is (locked single-worker assumption
stays). For the per-job fan-out the CLI knows exactly which two tasks
it just enqueued, so we route around `claim_one` — direct dispatch by
known task id, with one session per concurrent branch.

**Implementation in `cli/run.py`** (replaces the current `while True:
run_worker_once` drain for the discovery-run command):

```python
async def _claim_known_task(session: AsyncSession, task_id: int) -> Task | None:
    """Atomically flip THIS task's status queued→running. Returns None if
    the task is no longer queued (someone else got it / it's already done).

    This is the per-id analog of claim_one. It avoids claim_one's race
    by targeting one specific row, so concurrent callers cannot collide
    on the same task. Lives in workers/worker.py as a small additive
    helper (no edits to claim_one or run_one).
    """
    # UPDATE tasks SET status='running', claimed_at=NOW(), attempts=attempts+1
    # WHERE id=? AND status='queued' RETURNING *
    ...


async def _run_task_id(maker, registry, task_id: int) -> None:
    async with maker() as s:
        task = await _claim_known_task(s, task_id)
        if task is None:
            return
        await run_one(s, registry, task)


# In _run_discovery, after both enqueues:
reddit_task = await enqueue_reddit_task_for_job(session, job)
hn_task = await enqueue_hn_task_for_job(session, job)
console.print(f"queued reddit task: {reddit_task.id}")
console.print(f"queued hn task: {hn_task.id}")

# Close the planning session before fanning out — each branch opens
# its own.
# (the session is the one created at the top of _run_discovery)
await session.commit()

await asyncio.gather(
    _run_task_id(maker, registry, reddit_task.id),
    _run_task_id(maker, registry, hn_task.id),
)
```

**New worker helper.** `_claim_known_task` (the per-id analog of
`claim_one`) lives in `workers/worker.py` as a small additive
function. It does NOT modify `claim_one` or `run_one`. It is the
minimum addition that achieves correct parallel dispatch without
lifting the single-worker assumption. Approximate body (SQLite
`UPDATE ... RETURNING`):

```python
async def claim_known_task(session: AsyncSession, task_id: int) -> Task | None:
    """Atomically claim a SPECIFIC task by id (queued → running).

    The general-queue `claim_one` is single-worker-safe only. This
    per-id variant is race-safe under concurrent callers because the
    `UPDATE ... WHERE id=? AND status='queued' RETURNING *` is one
    atomic statement: at most one caller sees status='queued' and
    flips it. Used by cli/run.py's parallel fan-out.
    """
    stmt = (
        sqlmodel_update(Task)
        .where(Task.id == task_id, Task.status == TaskStatus.queued)
        .values(
            status=TaskStatus.running,
            claimed_at=datetime.now(UTC),
            attempts=Task.attempts + 1,
        )
        .returning(Task)
    )
    result = await session.exec(stmt)
    row = result.first()
    await session.commit()
    return row
```

(The exact SQLModel + SQLAlchemy update incantation will be finalized
in the plan; the spec asserts the semantics: race-safe per-id claim,
returns None if the task is no longer queued.)

**Partial success across sources.** If Reddit fails entirely and HN
succeeds, the job still produces HN `raw_records` and vice versa.
`asyncio.gather` runs both branches to completion regardless of
either's outcome; `run_one` already catches and finalizes failures
inside the branch. One source dying never kills the other.

## 13. Registry wiring (`src/discovery/workers/__init__.py`)

```python
def build_default_registry() -> SourceRegistry:
    from discovery.config.settings import settings
    from discovery.sources.hackernews import HackerNewsSource  # NEW
    from discovery.sources.reddit import RedditSource

    adapters: dict[str, BaseSource] = {
        "reddit": RedditSource(user_agent=settings.reddit_user_agent),
        "hackernews": HackerNewsSource(),  # NEW — no creds needed
    }
    return adapters
```

`aclose_registry` already iterates and calls `aclose`; `HackerNewsSource.aclose`
closes the owned client — no `aclose_registry` change needed.

## 14. The new project skill (`.claude/skills/hackernews-source/SKILL.md`)

The owner explicitly authorized this skill (CLAUDE.md normally guards
`.claude/`). Outline of contents — numbered like `reddit-source`, so
review comments and commits can cross-reference items by number:

1. **Use the Algolia API, don't scrape HTML.**
2. **Two endpoints, two purposes:** `/search` (relevance) vs
   `/search_by_date` (newest). Transport flag at plan time, URL built
   at fetch time.
3. **`tags` taxonomy.** `story`, `comment`, `ask_hn`, `show_hn`,
   `poll`, `front_page`, `author_X`, `story_X`. Comma=AND,
   parens=OR. Project uses `show_hn` for launch, `story` for context.
   Empirically verified (2026-05-20) that Ask HN and Show HN posts
   carry BOTH `story` AND their subtype tag in `_tags`, so
   `tags=story` is a true superset that catches Ask HN's pain-shaped
   "how do you handle X?" threads — the closest HN gets to Reddit-
   style problem discussion. No need to OR `(story,ask_hn)`.
4. **Strict token-AND on `query` — no OR.** Long phrases starve;
   decompose to ≤2 tokens. (Cross-ref `keyword_tokens.py`.)
5. **Decomposition policy.** Algorithm + stopword set + casing rule
   (acronyms preserved). Reusable later by GitHub/arXiv.
6. **Server-side `numericFilters` IS the quality floor.** `points`,
   `num_comments`, `created_at_i`. AND with commas. Relaxed for
   launch queries; tight for context queries.
7. **Query budget cap ~6.** More queries = more downstream LLM cost
   without proportional yield.
8. **Set `hitsPerPage` explicitly (30). No pagination.**
9. **Per-instance limiter, not a singleton.** Only one HN consumer;
   Algolia's ceiling is effectively unlimited; the reddit_ratelimit
   singleton's reason doesn't apply.
10. **No retry, partial success.** One GET per query; record errors
    per-query; raise only when all queries in a task fail. Documented
    divergence from the source-adapter umbrella's "retry with backoff".
11. **Bronze stores raw.** `objectID` → `external_id`; verbatim hit
    in `body`. Snippet/permalink fallback/missing-`url` handling are
    Wave 2 concerns in THIS project (even though the HN guide
    discusses them as adapter-side — that part of the guide is
    explicitly deferred to Wave 2 here).
12. **Capability not pain.** Downstream tagging concern (Wave 2). The
    LLM is taught the framing in the v6 prompt; the adapter just
    stores raw.
13. **Don't.** No `OR` keyword tricks. No long phrases. No
    lowercasing. No `/search`-only (you miss launches). No
    pagination. No NSFW filter / body trim (defer to Wave 2).
14. **Mental model.** Reddit = pain; HN = capability. Complementary.

**Divergences this skill explicitly documents** (so future sessions
don't "fix" them):

- The HN guide's adapter-side snippet construction, permalink
  fallback, and capability-tagging are **deferred to Wave 2** here.
- The source-adapter umbrella's "retry with backoff" is **deliberately
  not honored** for HN; project-locked partial-success across queries
  IS honored.
- Reddit's process-wide singleton limiter pattern is **deliberately
  not mirrored** for HN (only one consumer).

## 15. Tests

Mirror `tests/unit/sources/test_reddit.py` exactly in structure. New
test modules:

**`tests/unit/sources/test_keyword_tokens.py`** — pure:

- Splits on whitespace.
- Drops stopwords case-insensitively.
- Preserves casing of survivors (`MCP` stays `MCP`).
- Keeps first 2 surviving tokens.
- Empty input → `[]`; all-stopwords input → `[]`.
- Long phrase ("privacy preserving data collection library") → 2
  tokens.

**`tests/unit/sources/test_hackernews.py`** — pure helpers + adapter:

- `TestBuildSearchUrl` — `/search` vs `/search_by_date` routing,
  `tags=`, `numericFilters=` (commas, operators), `hitsPerPage=30`,
  no pagination, `query` URL-encoded.
- `TestKeepHit` — drops hits with no `objectID`; keeps everything else.
- `TestHitToRawRecord` — `external_id=str(objectID)`,
  `source="hackernews"`, `body == hit` verbatim, no
  trimming/normalization.
- `TestHackerNewsSourceFetch` — using `httpx.MockTransport`:
  - happy path returns records with the right `external_id` and
    `source`,
  - partial success: one query 500s → returns the others' records,
  - all queries fail → raises the first error,
  - text-post with null `url` still yields a valid `external_id`
    from `objectID`,
  - no retry: a single 500 records ONE error (not retried).
- `TestHackerNewsSourceLogging` — loguru sink asserts the per-query
  log line carries `url`, `status`, `elapsed_ms`,
  `count_before_filter`, `count_after_filter`, `endpoint`, `tags`.

**`tests/unit/orchestrator/test_hackernews.py`** — orchestrator:

- `enqueue_hn_task_for_job` idempotent on `(job_id, content_hash)`.
- Reads `job.job_plan["hn_queries"]` when present, calls
  `_compile_hn_queries`.
- Template fallback fires when `job_plan` is null OR when
  `hn_queries` is empty OR when decomposition wipes everything out.
- `_compile_hn_queries` routes by `intent` (launch→show_hn+by_date,
  context→story+search).
- `_compile_hn_queries` caps at `MAX_HN_QUERIES=6` preserving LLM
  order.
- `_compile_hn_queries` dedupes on the joined token tuple
  (case-sensitive: `MCP` and `mcp` are different).
- `_time_window_epoch` table maps correctly for each window;
  `all` → None.

**`tests/unit/llm/test_schemas.py` additions** —
`HackerNewsKeywordSpec` validation (`intent` Literal),
`JobPlan.hn_queries` permissive default (`default_factory=list`), and
the new `_attach_hn_queries` helper in `query_expansion.py` preserves
`hn_queries` past a simulated locked-tail rebuild.

**`tests/unit/workers/test_registry.py` (or wherever the registry test
lives)** — `build_default_registry()` includes a `"hackernews"`
adapter; `aclose_registry` closes it (use a probe adapter or
side-effect counter).

**`tests/unit/cli/test_run_parallel.py`** — the parallel fan-out
correctness test: stub both adapters with detectable delays, ensure
both branches run concurrently (start times overlap), both tasks
flip to `done`, and a failing Reddit branch does NOT prevent HN from
succeeding. Use the maker+session pattern the existing CLI tests use.

**`tests/unit/workers/test_claim_known_task.py`** — the new
`claim_known_task` helper:

- Atomically flips queued→running with `attempts=1`.
- Returns None when the task is already running (or completed).
- Two concurrent callers on the same task id: exactly one gets a
  row, the other gets None.

**No `conftest.py` autouse fixture for HN.** Per-instance limiter
means no shared state across tests. The Reddit autouse fixture stays
Reddit-specific.

**No VCR.** Project convention is `httpx.MockTransport`.

## 16. Documented divergences (so they're not "fixed" later)

- **From the source-adapter umbrella skill** (`.claude/skills/source-adapter/SKILL.md`):
  the umbrella says "Retried. Wrap the network call with
  `@tenacity.retry` — exponential backoff, max 3 attempts." HN does
  NOT retry. Documented in `hackernews-source` item 10.
- **From the HN guide** (the owner's playbook becoming the new skill):
  the guide discusses adapter-side snippet construction, permalink
  fallback for missing-`url`, and "capability not pain" tagging.
  Those are *Wave 2* concerns in this project (Bronze stores raw).
  Documented in `hackernews-source` item 11.
- **From the `reddit-source` skill**: Reddit uses a process-wide
  singleton limiter (`reddit_ratelimit.py`). HN deliberately does NOT
  mirror this pattern (only one HN consumer; Algolia's ceiling is
  effectively unlimited). Documented in `hackernews-source` item 9.
- **From the brainstorming default location**: spec goes to
  `docs/specs/` (project convention), not
  `docs/superpowers/specs/` (skill default). Owner preference wins.

## 17. Risks + invariants for future sessions to preserve

- **The locked Wave-0 tail must stay byte-for-byte Reddit-only.** If a
  future session edits any of the four `model_construct` helpers
  (`_ground_selection`, `_force_time_window`, `_merge_baseline_subreddits`,
  `_drop_invalid_queries`) OR their orchestrator (`_finalize`), they
  MUST preserve the property that `_attach_hn_queries` reattaches
  `hn_queries` afterward. The carry-through is fragile by design.
- **Single-worker assumption is NOT lifted.** Parallel HN+Reddit
  works because the CLI routes around `claim_one` via the new
  `claim_known_task`. Multi-job concurrent dispatch would require
  making `claim_one` itself race-safe (the worker comment already
  sketches the SQL); not in scope here.
- **Prompt `VERSION` v5→v6 invalidates the combined Wave-0 cache for
  all jobs.** First runs after the slice will be cold — expected.
- **Idempotency trap (unchanged).** Re-running the same
  `industry+location+as_of+time_window` returns the cached old job;
  `plan_job` short-circuits. To truly re-test the v6 prompt, change
  the industry or `--as-of`. Documented in CLAUDE.md / env gotchas.
- **HN sparsity on non-tech industries is graceful.** The HN guide
  is explicit: empty/partial is not a failure for trades / local-
  services industries. The §8 prompt now tells the LLM the same
  thing (the "Quality over quota" paragraph in "What to emit") so
  it emits fewer or zero candidates rather than manufacturing tech-
  shaped garbage. Tests assert that an HN task with zero records
  does NOT mark the task `failed`; it completes `done` with zero
  rows.
- **Tag values aren't validated against Algolia's enum.** If Algolia
  ever renames or drops `show_hn`, our hard-coded routing breaks.
  The risk is low (HN's tag set has been stable for ~a decade); if
  it changes we'll see it as a sustained zero-yield log signal.

## 18. Decision record

- **Strategy:** HN over Wave 2 (owner's call after the honest
  tradeoff was surfaced).
- **Fan-out behavior:** both Reddit and HN on every run, no flags
  (owner: "Both, every run").
- **Retry scope:** Minimal, HN-native; do NOT touch shipped Reddit
  code (owner: "Minimal, HN-native").
- **LLM contract for HN:** Approach A — LLM emits ranked
  `HackerNewsKeywordSpec` candidates (keyword + intent + rationale);
  Python owns all mechanics (decomposition, 2:1 routing,
  `numericFilters`, cap).
- **Carry-through:** capture `hn_queries` once after the LLM call,
  let the Reddit tail run untouched, reattach once before caching.
  Do NOT thread `hn_queries=` through the four `model_construct` helpers in the locked tail.
- **Limiter:** per-instance `AsyncLimiter(5, 1)`, not a singleton.
- **Skill creation:** `.claude/skills/hackernews-source/SKILL.md` is
  explicitly authorized by the owner.
- **Spec location:** `docs/specs/YYYY-MM-DD-<topic>-design.md`
  (project convention, overriding the brainstorming default).
- **Parallel execution:** direct concurrent dispatch by known task id
  in `cli/run.py`, via a new additive `claim_known_task` helper in
  `worker.py`. Single-worker assumption preserved.

## 19. Sources

- [HN Algolia Search API guide (Cotera)](https://cotera.co/articles/hacker-news-api-guide) — parameter names, response shape, hitsPerPage max.
- [Algolia hn-search repo](https://github.com/algolia/hn-search) — indexable attributes (`title, url, author, points, story_text, comment_text, num_comments, story_id, story_title`), tag schema (`item_type, author_{author}, story_{story_id}`).
- [Algolia `numericFilters` reference](https://www.algolia.com/doc/api-reference/api-parameters/numericFilters) — operator list (`<, <=, =, !=, >=, >`), AND/OR semantics (comma vs nested arrays/parentheses).
- The owner's HackerNews adapter playbook (provided in-session 2026-05-18) — converted to the new `.claude/skills/hackernews-source/SKILL.md`.

---

End of spec.
