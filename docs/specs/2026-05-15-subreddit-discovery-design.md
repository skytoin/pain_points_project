# Subreddit Discovery — Design Spec

**Date:** 2026-05-15
**Status:** Approved design, ready for implementation planning (build in a fresh session).
**Branch when written:** `claude/quirky-mcclintock-17ee22`
**Related skills (policy — read before building):**
- `.claude/skills/reddit-source/SKILL.md` — every numbered item still applies; this
  spec adds a sub-discovery path that must obey items 2, 3, 4, 10, 17, 20, 21.
- `.claude/skills/llm-station/SKILL.md` — both LLM calls follow the station
  contract (cached, Pydantic-validated, versioned prompt).
- `.claude/skills/source-adapter/SKILL.md` — the new Reddit `/subreddits/search`
  client is a source-style component (async, rate-limited, retried, validated)
  but returns subreddit metadata, NOT Bronze `RawRecord`s.

> **For the build session:** this is the design. Turn it into a step-by-step
> implementation plan with the `superpowers:writing-plans` skill. Do not start
> coding from this document directly — it specifies *what* and *why*, not the
> task-by-task *how*.

---

## 1. Problem & why

Wave 0 currently asks the LLM to name subreddits **from its training memory**.
That has two unfixable flaws:

- **Hallucination.** The LLM can emit confident but nonexistent names
  (`r/doggroomers` vs. the real `r/groomers`).
- **Staleness.** The LLM cannot know subreddits created after its training
  cutoff, and "knows" subs that were since banned or renamed.

Research into Reddit's API (2026-05-15) established the fix and its constraints:

- `/subreddits/search.json?q=<phrase>` returns **real, currently-existing**
  subreddits, unauthenticated, one request per phrase, `limit` up to 100.
- It matches substrings of **both `display_name` and `public_description`**, so
  recall is good but description-matching drags in off-topic giants — filtering
  is mandatory.
- Its `sort` parameter is **non-functional** — we cannot trust Reddit's
  ordering; all ranking/selection intelligence must be ours.
- The `t5` object exposes the signals we need: `subscribers`,
  `active_user_count` / `accounts_active`, `subreddit_type`, `over18`,
  `public_description`.
- **Private/archived/quarantined** subs return 403 unauthenticated and are
  unusable. **`restricted`** subs are READable (only posting is gated) and
  must be kept.

**Core design move:** the LLM stops *recalling subreddit names* and instead
*generates semantic search phrases* to find them. The LLM does what it is good
at (semantic expansion, reading descriptions, judging relevance); Reddit
supplies ground-truth names; deterministic code handles the rules Reddit's
broken `sort` won't.

---

## 2. Locked decisions (from the brainstorming dialogue)

1. **Folded inside Wave 0.** `run_query_expansion(spec) -> JobPlan` keeps its
   public signature and its single combined cache entry. Internally it becomes
   a multi-step process. (Consistent with the existing "inline Option A"
   choice; the documented Option-B promotion path still applies if Wave 0 is
   later moved to a worker task.)
2. **Two LLM calls inside Wave 0:**
   - **Call #1 — phrase generation.** Input: `JobSpec`. Output: a small set of
     semantic *subreddit-search phrases* (NOT subreddit names).
   - **Call #2 — grounded selection + query design.** Input: the filtered
     subreddit table + `JobSpec`. Output: a `JobPlan` (selected subreddits +
     content queries), using the existing v3 query-design rules.
3. **Deterministic middle.** Between the two LLM calls, pure code collects,
   dedupes, filters, and ranks candidate subreddits — no LLM judgement there.
4. **Selection is adaptive with a hard ceiling of 30.** Call #2 keeps every
   subreddit it judges clearly on-topic AND alive, ordered best→worst by its
   own confidence, with no minimum. If it returns more than 30, deterministic
   code keeps the top 30 in the LLM's order.
5. **The LLM in Call #2 may ONLY pick from the supplied table.** Never from
   memory, never invented. This is the whole point of the feature and is a
   non-negotiable prompt rule.

---

## 3. End-to-end flow

```
JobSpec
  │
  ▼
LLM Call #1  (prompt: subreddit_phrases, VERSION v1)
  → ~5 semantic subreddit-search phrases
  │
  ▼
For each phrase:  GET /subreddits/search.json?q=<phrase>&limit=100
                  &raw_json=1&include_over_18=false
  → up to ~500 raw t5 results total
  │
  ▼  (deterministic, no LLM)
dedupe to unique subreddits
  → while deduping, count distinct phrases that surfaced each = matched_phrases
drop non-public         (keep subreddit_type ∈ {public, restricted})
compute median(subscribers) over survivors
drop "drastically below median"   (subscribers < median / 10)
compute activity_ratio = active_user_count / subscribers   (missing active → 0)
project to the 6-column table
  │
  ▼
LLM Call #2  (prompt: query_expansion, VERSION v4)
  → reads the table, selects relevant subs (ordered best-first, ≤30),
    AND designs 10–15 content queries (existing v3 rules UNCHANGED: ≤6 subs/
    site-wide query, 8 pain categories, uppercase OR/AND, quoted phrases,
    per-query rationale). The JobPlan schema constraint
    reddit_queries=min_length=10,max_length=15 is RETAINED — see §10.
  │
  ▼  (deterministic — existing steps, order preserved)
if >30 subs selected → keep top 30 in the LLM's order
_drop_invalid_queries  (existing reddit_query_validator)
MIN_VALID_QUERIES check (existing: <10 valid → QueryExpansionError → fallback)
_force_time_window      (existing Item-11 step: override every query's t)
_merge_baseline_subreddits (existing Item-9 step)
  │
  ▼
JobPlan  → cached under the combined Wave 0 cache key → Job.job_plan
```

If any step fails or yields nothing usable → raise `QueryExpansionError`
(existing type) → `plan_job` leaves `job_plan` null → the Reddit orchestrator
falls back to the deterministic template (existing behaviour, unchanged).

---

## 4. Components & proposed file layout

| Concern | Location (proposed) | Notes |
|---|---|---|
| Phrase-gen prompt | `src/discovery/llm/prompts/subreddit_phrases.py` | New. `VERSION`, `SYSTEM_PROMPT`, `build_user_message(spec: JobSpec) -> str` |
| Selection+query prompt | `src/discovery/llm/prompts/query_expansion.py` | Existing file; bump `VERSION` v3 → **v4**, add grounding section. **Builder signature changes**: `build_user_message(spec: JobSpec)` → `build_user_message(spec: JobSpec, table: list[SubredditCandidate]) -> str` (renders the 6-column table into the user message). Every call site must be updated. |
| Reddit sub-search client | `src/discovery/sources/reddit_subreddits.py` | New. Async, rate-limited via the **shared** limiter (see §11), retried, returns `SubredditCandidate` DTOs. NOT a `BaseSource`, does NOT write Bronze. **`SubredditCandidate` is defined in THIS module**, not `schemas.py`. |
| Candidate DTO | `src/discovery/sources/reddit_subreddits.py` | `SubredditCandidate` lives with the client that produces it. `schemas.py` stays station-OUTPUTS-only per the llm-station file-layout rule; `JobPlan` / `RedditQuerySpec` unchanged in shape. |
| Deterministic pipeline | `src/discovery/llm/stations/query_expansion.py` | Existing station; add dedupe/consensus, filter, median, activity_ratio, table projection, overflow trim, orchestration of the two LLM calls. Watch the 600-line file ceiling — split helpers into `subreddit_selection.py` if it grows. |
| Station entry point | `src/discovery/llm/stations/query_expansion.py::run_query_expansion` | Signature unchanged. |

**Why the sub-search client is not a `BaseSource`:** `BaseSource.fetch`
returns `list[RawRecord]` destined for the `raw_records` Bronze table.
Subreddit discovery is a *planning artifact*, not Bronze data — it must not
land in `raw_records`. It still honours the source-adapter contract
(async/httpx, rate-limited, `@tenacity` retry, Pydantic-validated response)
and the reddit-source skill (User-Agent, 6.1s pacing, 429 policy, partial
success, per-request logging), but returns a distinct DTO.

---

## 5. Data structures

### `SubredditCandidate` (new, in `src/discovery/sources/reddit_subreddits.py`)

Internal DTO for one deduped, surviving subreddit. Lives with the client that
produces it (NOT in `schemas.py`, which the llm-station skill reserves for
station *outputs* — `SubredditCandidate` is an internal planning DTO, not the
station output; `JobPlan` remains the only station output). Six fields are
projected into the table the LLM sees; `subreddit_type`/`over18` are carried
for filtering only and dropped before the LLM.

```
name: str                 # display_name, no r/ prefix
subscribers: int
active_user_count: int    # missing/None in API → 0
activity_ratio: float     # computed: active_user_count / subscribers, ~4dp
public_description: str    # whitespace-collapsed, truncated ~300 chars
matched_phrases: int       # how many distinct phrase searches surfaced it
subreddit_type: str        # filter-only, NOT shown to LLM
over18: bool               # filter-only, NOT shown to LLM
```

### Table passed to LLM Call #2 (exactly these 6 columns, in order)

`name`, `subscribers`, `active_user_count`, `activity_ratio`,
`public_description`, `matched_phrases`.

Rendered compactly (e.g. one row per line, fixed delimiter) — NOT raw JSON.
Rationale: 25 raw `t5` objects ≈ 80k tokens; the projected table is a few
hundred. Compaction is mandatory, not an optimization.

### `JobPlan` / `RedditQuerySpec`

Unchanged in shape (still the v3 schema: `reddit_queries`,
`reddit_subreddits`, per-query `subreddit`, `t`, `rationale`, etc.). Only the
*prompt* that produces it changes (v4).

---

## 6. The two prompts (structural requirements, not final prose)

### Prompt #1 — `subreddit_phrases` (new, VERSION `v1`)

- **Task:** given the industry (+ optional location/size), produce ~5 distinct
  semantic phrases to search Reddit's *subreddit* index — phrases likely to
  surface communities of practitioners, customers, and adjacent niches.
- **Must convey:** these are *search phrases to find subreddits*, NOT subreddit
  names; vary angle (the trade itself, practitioner slang, the customer side,
  adjacent verticals); keep each phrase short (Reddit `q` for sub-search is
  short — well under any limit at ~5 phrases).
- **Output:** Pydantic model, e.g. `SubredditSearchPhrases(phrases: list[str]
  = Field(min_length=3, max_length=8))`. This is a station output and DOES
  belong in `schemas.py`.

### Prompt #2 — `query_expansion` (existing file, bump to VERSION `v4`)

Carries **all** v3 content-query rules unchanged (uppercase OR/AND, quoted
phrases, the 8 pain categories, per_sub vs site_wide, **≤6 subreddits per
site-wide query**, honour `time_window`, mandatory per-query `rationale`,
JobPlan output). **Adds a grounding section:**

- **Hard rule:** "These are the ONLY subreddits available for this job. Select
  exclusively from this table. Never use a subreddit not listed. Never invent
  names. If the table is thin, use *fewer distinct subreddits* — but you must
  STILL produce 10–15 content queries by varying the pain-phrase angle across
  the available subs (per_sub and site_wide combinations). Do NOT fall back to
  your own knowledge, and do NOT emit fewer than 10 queries."
  (Query count is driven by subreddit × pain-category combinations, not 1:1
  with subreddit count — even 3 subs comfortably yield 10–15 queries. The
  existing `JobPlan.reddit_queries` `min_length=10, max_length=15` constraint
  and `MIN_VALID_QUERIES=10` floor are UNCHANGED — see §10.)
- **How to read the table + the traps:**
  - `public_description` is the **primary relevance signal** — does the sub's
    stated purpose match the industry, or does it merely contain the word?
  - `matched_phrases` high ⇒ robustly on-topic; `=1` ⇒ likely a fluke
    description match, treat with suspicion.
  - `activity_ratio` is misleading on tiny subs — always cross-check raw
    `active_user_count` (12 active people is thin regardless of ratio).
  - Large `subscribers` ≠ better — prefer a practitioner community
    (`r/groomers`) over a generic mega-sub (`r/dogs`).
- **Selection instruction:** keep every sub that is on-topic AND alive; **order
  the selection best→worst by your confidence**; there is no minimum; the hard
  ceiling is 30 (more than that will be trimmed to your top 30).

---

## 7. Deterministic pipeline rules (exact)

Order is fixed; each step is independently unit-testable.

1. **Collect.** For each phrase, one `GET /subreddits/search.json` request
   (`limit=100`, `raw_json=1`, `include_over_18=false`). Partial success per
   reddit-source skill item 17: if some phrase requests fail but ≥1 succeeds,
   proceed with what returned; only hard-fail if zero subreddits collected.
2. **Dedupe + consensus.** Collapse to unique `name`. `matched_phrases` =
   count of *distinct phrases* whose result set contained this sub. (Dedup
   MUST precede the median — duplicates would skew it.)
3. **Drop non-public.** Keep `subreddit_type ∈ {public, restricted}`. Drop
   `private`, `archived`, `quarantined`/anything else.
4. **Drop NSFW.** Drop `over18 == true` (defense-in-depth — `include_over_18=
   false` is also set on the request per skill item 12; neither alone is
   fully reliable). After this step `over18` has served its purpose.
5. **Median.** `median(subscribers)` over the survivors of step 4.
6. **Drop drastically-below-median.** Deterministic rule:
   `subscribers < median / 10` → drop. (Gentle relative floor: kills dead/junk
   without decapitating small niche communities the LLM should still get to
   judge. The divisor `10` is a named constant `DRASTIC_FLOOR_DIVISOR`; tune
   later if data warrants.)
7. **activity_ratio.** For each survivor:
   `activity_ratio = active_user_count / subscribers` (guard: missing/None
   active → 0; subscribers is > 0 here because step 6 dropped near-zero subs,
   but still guard divide-by-zero defensively → 0.0).
8. **Project** to the 6-column table; render compactly.
9. **(After LLM Call #2) Overflow trim.** If the LLM returned >30 selected
   subreddits, keep the **first 30 in the LLM's emitted order**. Pure-code
   tie-break only if order is somehow ambiguous: `matched_phrases` desc, then
   `activity_ratio` desc.
10. **(After overflow trim) Existing deterministic tail — UNCHANGED, order
    preserved from current `run_query_expansion`:** `_drop_invalid_queries`
    (reddit_query_validator) → `MIN_VALID_QUERIES` check (<10 valid →
    `QueryExpansionError`) → `_force_time_window` (Item-11 `t` override) →
    `_merge_baseline_subreddits` (Item-9). The new discovery logic slots in
    *before* this tail; the tail itself is not modified.

---

## 8. Caching & versioning

- **One combined Wave 0 cache entry** (unchanged contract: "cached for the
  whole job").
- **Use the EXISTING `discovery.llm.cache.cache_key()` API unchanged** — it
  takes `spec=`, `prompt_version=` (a single string), `model=`. Do NOT invent
  a new dict-shaped key and do NOT use `discovery.hashing.hash_params`
  directly (that's the orchestrator's task-hash, a different function). The
  current call is:
  `cache_key(spec=spec.model_dump(mode="json"), prompt_version=query_expansion.VERSION, model=MODEL)`.
  **Change only the `prompt_version` argument** to a combined string so both
  prompt versions participate:
  `prompt_version=f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}"`.
  Bumping either VERSION changes the combined string → fresh full re-run
  (re-phrase, re-search, re-select). This preserves the existing
  "VERSION bump invalidates cache" property across both calls with zero
  change to the cache module.
- Sub-discovery is **not** separately cached — it lives inside the cached
  Wave 0 result. A cache hit skips phrase-gen, all Reddit sub-searches, and
  selection in one shot.

---

## 9. Rate budget & runtime consequence (state explicitly)

A cold Wave 0 now costs, in sequence (all inside the inline `plan_job` call):

- LLM Call #1 (~10–30 s)
- ~5 Reddit `/subreddits/search.json` requests through the **shared 10/min
  limiter** (~5 × 6.1 s ≈ 30 s)
- LLM Call #2 (~25–35 s)
- Then Wave 1 content fetch: up to ~5 site-wide queries (≤6 subs each for ~30
  subs) × 6.1 s ≈ 30 s

≈ **~2 minutes wall-clock per cold run**; cached runs are near-instant. The
sub-search requests and the content-fetch requests share one rate limiter, so
they serialize — this is acceptable and correct (skill items 3 & 5: budget
around the limit, don't burst). This reinforces, but does not change, the
existing inline-Option-A decision; the Option-B (worker task) promotion path
documented in `docs/handoff.md` becomes more attractive if cold-run latency
becomes a problem at scale.

---

## 10. Fallback & error handling

Single principle: **any failure in the discovery+expansion chain raises
`QueryExpansionError`**, which `plan_job` already catches → `job_plan` stays
null → Reddit orchestrator uses the deterministic template. No new fallback
branches; reuse the proven degradation path.

Failure points and handling:

| Failure | Handling |
|---|---|
| LLM Call #1 fails / invalid | `QueryExpansionError` → template fallback |
| Some phrase searches 429/5xx | partial success (skill 17): proceed if ≥1 ok |
| All phrase searches fail | `QueryExpansionError` → template fallback |
| Zero subs survive filtering | `QueryExpansionError` → template fallback |
| LLM Call #2 fails / invalid | `QueryExpansionError` → template fallback |
| LLM Call #2 picks subs not in table | reject those subs (defensive filter); if too few remain → `QueryExpansionError` |
| Content queries fail validation | existing `reddit_query_validator` drop; existing min-query floor triggers fallback |

Empty sub-search result for one phrase is **not** an error (skill item 20) —
it's `ok_empty`; only a total wipeout fails.

### Reconciling the ≥10 query floor with adaptive thin-table selection

This is the subtle one. The existing code enforces `MIN_VALID_QUERIES = 10`
and `JobPlan.reddit_queries` is `Field(min_length=10, max_length=15)`. The new
adaptive selection can legitimately yield a *small subreddit set* on niche
industries. **These do not conflict, and the floor/schema stay UNCHANGED**,
because query count is NOT 1:1 with subreddit count:

- Content queries are subreddit×pain-category combinations. The 8 pain
  categories (skill item 8) plus per_sub/site_wide variants mean even **3
  subreddits comfortably produce 10–15 distinct queries**.
- Prompt v4 explicitly instructs the LLM: thin table → fewer *distinct
  subreddits*, but STILL 10–15 queries by varying the pain-phrase angle.
- The 30-sub ceiling ÷ ≤6 subs per site-wide query also sits naturally inside
  the 10–15 query band (≥5 site-wide + per_sub queries).

So a niche industry yields *fewer subreddits but the same 10–15 queries*, and
does NOT auto-fallback. The `MIN_VALID_QUERIES` check only trips if the LLM
genuinely fails to produce 10 valid queries (a real failure worth falling back
on), exactly as today. **No schema change, no floor change.** If real-world
data later shows niche industries genuinely cannot sustain 10 quality queries,
relaxing `min_length` is a separate, deferred decision (see §13) — not part of
this spec.

---

## 11. reddit-source skill compliance checklist

The new `/subreddits/search` client MUST honour:

- **Item 2** — descriptive `User-Agent` from settings (reuse existing).
- **Item 3 — shared limiter ownership (explicit, this is a real wiring gap).**
  Today `RedditSource.__init__` default-constructs its OWN `AsyncLimiter`
  (`AsyncLimiter(10, 60.1)`) and the station constructs none. Sub-search
  (Wave 0) and content-fetch (Wave 1) run in the same process and share ONE
  10/min Reddit budget — they must share ONE limiter instance. **Instruction:**
  introduce a single process-wide Reddit limiter (e.g. a module-level
  `get_reddit_limiter()` singleton in a small `discovery/sources/reddit_ratelimit.py`).
  `RedditSource` defaults to it instead of constructing its own; the new
  sub-search client uses the same singleton. Tests inject a fake/no-op limiter
  as today. Do NOT let either component default-construct a separate limiter —
  that silently doubles the request rate and invites 429s.
- **Item 4 — and 403 MUST raise, not return empty.** 401/403 = denied: the
  client must surface this as an exception (call `response.raise_for_status()`
  or raise explicitly), NOT return an empty list. A 403 silently mapped to
  "0 results" would be indistinguishable from a legitimate empty search
  (skill item 20) and would mask an auth/IP block. 429 = retry w/ Retry-After
  honoured & clamped; 5xx/network = transient retry; cap 3 attempts. (Mirror
  the existing `_fetch_with_retries`, but ensure the 4xx path raises before
  results are interpreted.)
- **Item 10** — defensively re-validate any subreddit name format before it
  reaches a content query (subs now come from Reddit so they're real, but the
  validator stays as belt-and-suspenders).
- **Item 17** — partial success across phrase requests (one phrase 403/429ing
  after retries does not kill the others; only a total wipeout fails).
- **Item 20** — empty result set for a phrase ≠ failure (a 200 with empty
  `children` is `ok_empty`; distinct from the 403-raises case above).
- **Item 21** — per-request structured log: URL, status, elapsed_ms, result
  count before/after filtering (mirror the content adapter's logging shipped
  in the Items 9/11/21 work).

---

## 12. Testing strategy

All tests offline (no real network / no real LLM), mirroring existing patterns:

- **Deterministic pipeline (highest value, pure functions):** dedupe +
  `matched_phrases` counting; non-public filter (public/restricted kept,
  private/archived dropped); median calc; drastically-below-median drop
  (boundary cases around `median/10`); `activity_ratio` incl. missing-active
  and divide-by-zero guard; table projection (exactly 6 columns, truncation,
  whitespace collapse); overflow trim (≤30 passthrough, >30 keeps LLM order).
- **Reddit sub-search client:** `httpx.MockTransport` (as existing reddit
  tests); 200 happy path → DTOs; 429 retry; 403 denied; partial success;
  empty `children`; per-request logging assertion (loguru sink, as Item 21
  test).
- **Station orchestration:** monkeypatch both LLM calls + the sub-search
  client; assert cache hit skips everything; cache miss runs the full chain;
  every fallback row in §10's table raises `QueryExpansionError`; LLM picking
  an off-table sub is rejected.
- **Prompts:** shape tests only (VERSION present/bumped, builder renders spec
  fields, grounding rule + column guide substrings present) — do not pin
  wording.

Target: full chain green offline; a manual `discovery run` smoke on one niche
and one rich industry after build, reading the Item-21 logs to confirm
sub-search yields and that selection respects the table.

---

## 13. Open / deferred items & risks

- **`median/10` divisor** is a first guess. Acceptable to ship; revisit once
  the Item-21 logs show real subscriber distributions per industry.
- **Persisting the candidate table** for debugging is deferred — log it
  (Item-21 style), don't store it on the Job, unless a later need appears
  (YAGNI).
- **Phrase count (~5)** is a starting point; it sets the "≈500 results" and
  the rate budget. Tunable via the prompt without code change.
- **Risk — description-match noise survives filtering.** Mitigated by: the LLM
  reading `public_description` as the primary relevance signal, plus
  `matched_phrases` exposing fluke single-phrase matches. Acceptable; monitor.
- **Risk — cold-run latency (~2 min).** Accepted under inline Option A;
  Option-B promotion path already documented if it bites.
- **Interaction with existing Item-9 baseline merge:** baseline subs
  (`r/startups`, `r/microsaas`, `r/smallbusiness`) are still merged AFTER
  selection, as today — they are a safety net independent of discovery. Keep
  that step; it does not conflict.
- **Deferred — relaxing `JobPlan.reddit_queries` `min_length=10`.** Out of
  scope here (see §10 reconciliation: thin tables still yield 10–15 queries
  via pain-category variety). Only revisit if Item-21 data proves niche
  industries genuinely can't sustain 10 quality queries. A schema change is
  its own spec.
- **Shared Reddit limiter is a prerequisite refactor.** Introducing the
  process-wide limiter singleton (§11 item 3) touches existing `RedditSource`
  construction. It's small but it is a real change to shipped code, not
  net-new — call it out in the plan as step 0 so it's reviewed deliberately.

---

## 14. Suggested build sequence (high level — detailed plan via writing-plans)

0. **Prerequisite refactor:** introduce the process-wide Reddit limiter
   singleton (`discovery/sources/reddit_ratelimit.py`); make `RedditSource`
   default to it instead of self-constructing; keep test injection working.
   Existing reddit tests stay green. Own commit.
1. `SubredditCandidate` DTO (in the sub-search client module) + the
   deterministic pipeline pure functions (dedupe/consensus → drop non-public →
   drop NSFW → median → drop drastically-below → activity_ratio → projection →
   overflow trim), fully unit-tested in isolation.
2. Reddit `/subreddits/search` client (DTO return, **shared limiter from step
   0**, 403-raises, skill compliance, mocked-transport tests).
3. Prompt #1 module (`subreddit_phrases`, v1, output schema in `schemas.py`) +
   shape tests.
4. Prompt #2: add grounding section, change `build_user_message` signature to
   take the table, bump `query_expansion` to **v4**; update all call sites;
   shape tests.
5. Wire the two LLM calls + deterministic middle into `run_query_expansion`;
   combined `prompt_version` string in the existing `cache_key` call; preserve
   the existing deterministic tail (`_drop_invalid_queries` →
   `MIN_VALID_QUERIES` → `_force_time_window` → `_merge_baseline_subreddits`);
   full fallback table; station tests.
6. `/run-checks`; manual smoke on one niche + one rich industry; read Item-21
   logs; update `docs/handoff.md`.

Each step ends green (tests + ruff + mypy) and is its own commit, per the
project's existing TDD cadence.
