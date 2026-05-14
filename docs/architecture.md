# Architecture

This is the single source of truth for how the pipeline works. When you
work on a task that touches more than one wave, read the relevant section
here first.

---

## The pipeline at a glance

```
┌─────────────────────────────────────────────────────────────┐
│ JOB SETUP                                                   │
│   User submits fuzzy spec → 🤖 LLM Query Expansion          │
│                              (one call, cached for job)     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1 — Industry-wide Discovery   [12 sources, parallel]   │
│   Reddit • YouTube • HN • Apollo • Google Places • Yelp •   │
│   OpenCorporates • Trade Dirs • NewsAPI • Listen Notes •    │
│   Product Hunt • Census                                     │
│   ALL raw responses → Bronze layer                          │
│   No LLM calls. Pure HTTP + Pydantic + SQL.                 │
└─────────────────────────────────────────────────────────────┘
                              │  (barrier — wait for all)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2 — Resolve & Structure       [sequential, batched]    │
│   Step A: Deterministic fuzzy company match (rapidfuzz)     │
│   Step B: 🤖 LLM Entity Resolution (batched, ambiguous only)│
│   Step C: Write canonical companies table                   │
│   Step D: 🤖 LLM Pain Signal Classification (batched)       │
│   Step E: Write pain_signals + tools_mentioned tables       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ WAVES 3 & 4 — Per-Company + Per-Tool Enrichment  [parallel] │
│   Per company: reviews • jobs • news • tech stack • emails  │
│   Per tool: G2 / Capterra / Product Hunt detail             │
│   Inside Wave 3: 🤖 LLM Job-to-Task Extraction (batched)    │
└─────────────────────────────────────────────────────────────┘
                              │  (barrier — wait for both)
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ WAVE 5 — Link, Sanity-Check, Aggregate    [sequential]      │
│   Step A: SQL — link signals ↔ companies ↔ tools            │
│   Step B: Rule-based outlier detection (deterministic)      │
│   Step C: 🤖 LLM Sanity Check on flagged rows (batched)     │
│   Step D: Compute aggregates, mark job done                 │
└─────────────────────────────────────────────────────────────┘
```

Four LLM "stations" — Query Expansion, Entity Resolution, Pain
Classification, Sanity Check — plus one inline LLM call for Job-Task
Extraction during Wave 3. Everything else is plain code.

---

## Wave-by-wave walkthrough

### Wave 0 — Job Setup

**Input:** Fuzzy user spec like
`{industry: "commercial cleaning", location: "NY", size: "medium"}`.

**🤖 LLM Station #1: Query Expansion.** One call. Takes the fuzzy spec
plus a Pydantic model defining what each source needs. Returns a
`JobPlan` object containing source-specific parameters:

- `apollo_params`: NAICS codes, employee ranges, location filters
- `google_places_queries`: a list of search strings to fire
- `yelp_params`: categories, location, terms
- `reddit_subreddits`: ranked list of relevant subs
- `youtube_queries`: search phrases for pain-rich videos
- `news_keywords`: industry terms for NewsAPI
- …and so on

Validated against a strict Pydantic schema. Cached for the entire job under
a hash of the input spec. Cost: ~$0.01.

If the LLM call fails or returns invalid output, a deterministic fallback
uses a hand-written mapping table for the top 10 industries.

**Output:** A `jobs` row, status `running`, with the full `JobPlan` stored
as a JSON column.

### Wave 1 — Industry-wide Discovery (parallel)

The orchestrator reads `JobPlan` and fans out one task per source into the
`tasks` table. Worker pool picks them up. No LLM calls.

Each source adapter:

- Reads its parameters from `JobPlan`
- Hits the API with `httpx` (async)
- Rate-limited by `aiolimiter` per-source
- Retried by `tenacity` on transient failures
- Each response stored verbatim in `raw_records` (Bronze)
- Marks its task done

Pure plumbing. Twelve sources in parallel. Wall-clock time: as long as the
slowest source, typically 30–90 seconds.

**Barrier:** orchestrator polls `tasks` until Wave 1 has zero pending tasks.

### Wave 2 — Resolve & Structure (sequential, the brain wave)

This is where Bronze becomes Silver. Three substeps:

**Step A — Deterministic company clustering.** Pull all company-shaped
records from Bronze. Compute a `company_key` candidate for each
(slugified domain, or normalized name + zip). Cluster matches with
`rapidfuzz`. 80–90% of companies resolve cleanly.

**🤖 LLM Station #2: Entity Resolution.** The ambiguous 10% — pairs where
rapidfuzz returns 70–90% similarity — get sent to the LLM in batches of
20 pairs per call. Returns a structured
`same_entity: bool, confidence: float, reasoning: str` per pair. Resolved
companies merged into canonical clusters.

**Step C — Write canonical companies.** For each cluster, upsert into the
`companies` table using the natural key. Every constituent Bronze record
gets a row in the `company_facts` log so you have field-level provenance.

**🤖 LLM Station #3: Pain Signal Classification.** All pain-bearing text
from Wave 1 (Reddit posts, YouTube comments, HN threads, news excerpts) is
batched 25 per LLM call. Returns structured rows:

```python
class PainExtraction(BaseModel):
    pain_topic: Literal["scheduling", "billing", "follow_up",
                        "lead_response", "staffing", "tools", "other"]
    tools_mentioned: list[str]
    company_mentions: list[str]
    sentiment: Literal["negative", "neutral", "positive"]
    severity: int = Field(ge=1, le=5)
    industry_signal_strength: float = Field(ge=0, le=1)
    quote: str  # the most representative sentence
```

Validated by Pydantic on the way back. Cached by content hash so re-runs
cost nothing.

**Step E — Write Silver tables.** `pain_signals` gets one row per
classified text. `tools_mentioned` gets one row per `(signal, tool)` pair.
Tool names that don't already exist in the `tools` table go into a
`tools_unverified` queue for review (not auto-created — that's how you
avoid hallucinated tool names becoming canonical).

### Waves 3 & 4 — Per-Company + Per-Tool Enrichment (parallel)

The orchestrator fans out per-company and per-tool tasks. Workers pick
them up. Mostly pure code:

- Yelp reviews, Google reviews — HTTP, JSON, Pydantic, write to `reviews`
- Glassdoor (via Apify), Trustpilot (via Apify) — same pattern, just via Apify SDK
- Greenhouse/Lever public APIs — free, no auth
- TheirStack — paid, by domain
- Wappalyzer — tech stack
- NewsAPI — by company name

**🤖 LLM Station #4: Job-to-Task Extraction.** This one is inline within
Wave 3. Each job posting's description is sent to the LLM (batched 30 per
call) to extract a structured task list:

```python
class JobTasks(BaseModel):
    role_summary: str
    extracted_tasks: list[str]
    automation_friendly_tasks: list[str]
    tools_required: list[str]
    role_type: Literal["admin", "ops", "sales", "field", "other"]
```

Stored as a JSON column on `job_postings` and exploded into a `job_tasks`
table for easy SQL counting.

**Barrier:** wait for all per-company and per-tool tasks to be done or failed.

### Wave 5 — Link, Sanity-Check, Aggregate

**Step A — Cross-linking (pure SQL).**

- For each `tools_mentioned.tool_name`, resolve to a row in `tools` (if it
  exists). Insert `signal_tool_links`.
- For each `company_mentions`, fuzzy-match against `companies`. Confidence
  threshold 0.9. Insert `signal_company_links`.
- For each `job_tasks.extracted_task`, run text similarity against existing
  `task_categories` to bucket them.

**Step B — Rule-based outlier detection.** Quick deterministic checks:

- `employee_count > 100000` for SMB-targeted vertical → suspect
- name contains "coming soon", "placeholder", "test" → suspect
- address is residential pattern → suspect
- `review_count` field mismatches `COUNT(*) FROM reviews` → suspect
- domain is `*.wix.com` or `*.squarespace.com` with no own domain → flag as small-shop
- Same person email across 10+ different companies → suspect data quality

Flagged rows get `quality_flag = 'needs_check'`.

**🤖 LLM Station #5: Sanity Check.** Flagged rows go to the LLM in batches
of 25 with the rule that triggered. The LLM returns:

```python
class QualityVerdict(BaseModel):
    verdict: Literal["legitimate", "suspect", "delete_candidate"]
    reason: str
    suggested_fix: Optional[str]
```

Rows marked `suspect` get `quality_flag='suspect'` (kept but excluded from
default queries via a view). Rows marked `delete_candidate` are **not**
deleted — just hidden behind `quality_flag='excluded'`. Always reversible.

**Step D — Aggregate computation (pure SQL).**

- `companies.review_count`, `companies.avg_rating`, `companies.last_seen`
- `tools.mention_count`, `tools.negative_mention_count`
- `industries.signal_strength`

Mark job done. Pipeline complete.

---

## How LLM calls plug into the worker model

Every LLM call follows the same pattern, regardless of station:

```
1. Gather inputs (a batch of items)
2. Compute content_hash of the batch + prompt version + model
3. Check llm_cache — if hit, return cached result
4. Build prompt with few-shot examples + Pydantic schema
5. Call LLM with temperature=0, structured output enforced
6. Validate response against Pydantic model (auto-rejects malformed)
7. Store result in llm_cache keyed by hash
8. Return validated objects to the worker
```

LLM calls are just another task type in the `tasks` table. They have their
own rate limit (Anthropic's per-org limit), their own retry policy, their
own concurrency cap (e.g., 5 parallel LLM calls). The worker doesn't know
or care that some tasks call APIs and others call models — they're all
"tasks with inputs and outputs."

This means LLM failures don't block the rest of the pipeline. A pain-signal
classification task that fails retries 3× then marks itself failed —
Wave 2 proceeds with whatever did classify, and you can re-run failed ones
later.

---

## Principles to live by

- **LLM calls are tasks, not function calls.** They go through the same
  queue, same retry logic, same rate limits as HTTP tasks. This is what
  makes the pipeline resilient.
- **Every LLM output is Pydantic-validated.** No "parse the response and
  pray." If the LLM can't return valid structured data, the task fails
  cleanly and gets retried.
- **Cache every LLM call.** Content hash + prompt version + model = key.
  Same input never costs twice.
- **Deterministic-first, LLM-second.** Inside Wave 2, rapidfuzz handles
  90% of company matching. The LLM only sees the hard 10%. Same pattern
  for sanity checks — rules find suspects, LLM verifies.
- **LLM-extracted entities don't auto-create canonical rows.** If the LLM
  extracts "ServiceMaxxer" as a tool but it's not in your `tools` table,
  it goes to `tools_unverified` — never silently created. This is your
  guard against hallucination polluting the database.
- **Prompt versions live in code, not in your head.** Every prompt file
  exports a `VERSION` constant. The cache key includes it. When you tweak
  a prompt, you get a clean re-classification without breaking old data.
