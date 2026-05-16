# Session handoff — discovery pipeline

**Last touched:** 2026-05-16
**Branch:** `claude/jovial-poitras-fd083f`. The **subreddit-discovery
slice** (grounded Wave 0: search *phrases* → `/subreddits/search` →
deterministic rank → grounded selection) was merged to `main`
(`ebe2bc2`). On top of that, this branch now carries the **wider
query-band slice** (band 25–30 + industry-specific brainstorm, prompt
v5) — not yet merged. See the two dated "what shipped" sections below,
newest first.

Read this first when picking the project back up. It tells you what
exists, what decisions are locked in, and exactly where the next slice
starts.

---

## What the project is (one paragraph)

A data discovery pipeline that takes a fuzzy industry spec (e.g.
*"commercial cleaning, NY, medium-sized companies, as of June 2026"*)
and produces a structured dataset of companies, pain signals, tools,
and job-task patterns by scanning twelve external sources, classifying
the results through four LLM stations, and writing everything to
SQLite. See `docs/architecture.md` for the wave-by-wave walkthrough.

---

## What runs end-to-end today

You can do this **right now**, from the command line, with real Reddit
traffic:

```bash
$ uv run python -m discovery.cli.init_db          # one-time DB setup
$ uv run discovery run --industry "commercial cleaning" --location NY
job: 1  (spec_hash a4c1be3f1d2b…, status queued)
wave 0: planned             # ← LLM hit OpenAI gpt-5.4 (or "fallback")
queued task: 1  (source=reddit, queries=12)
  ✓ processed task 1
done. 1 task(s) processed.
```

After running, `data/discovery.db`'s `raw_records` table holds real
Reddit posts. Wave 0 is now a multi-step grounded process inside the
unchanged `run_query_expansion(spec) -> JobPlan` signature:
**JobSpec → plan_job → [LLM Call #1: subreddit-search *phrases* →
`/subreddits/search.json` per phrase → deterministic middle
(dedupe+consensus → drop non-public/NSFW → median → drop
drastically-below-median → activity_ratio) → LLM Call #2: grounded
selection + v5 query design → off-table reject + ≤30 trim → unchanged
tail (`_drop_invalid_queries` → MIN_VALID_QUERIES → `_force_time_window`
→ `_merge_baseline_subreddits`)] → one combined cache entry keyed by
`subreddit_phrases.VERSION + query_expansion.VERSION` → `Job.job_plan`
→ run_worker_once → RedditSource.fetch → raw_records rows.**

When `OPENAI_API_KEY` is unset, any LLM call fails, the sub-search
totally wipes out, zero subs survive filtering, or too few content
queries pass validation, the station raises `QueryExpansionError`,
`wave 0` prints `fallback`, and the Reddit orchestrator uses the
deterministic hand-rolled template — exactly as before. The proven
degradation path is reused; no new fallback branches. A combined-key
cache hit skips phrase-gen, every sub-search, and selection in one
shot (verified ~4 s vs ~2 min cold).

**Test counts:** 231 unit tests, all green (229 after the
subreddit-discovery slice; +2 net from the wider query-band slice).
`ruff check`, `ruff format --check`, `mypy src/` (strict), and
`pytest` all pass.

---

## Commit history (newest first)

| SHA       | Slice |
|-----------|---|
| `7aa7b9c` | `docs(station): correct stale v4/10-15/min_length refs after the v5 band change` |
| `1eb7b6d` | `feat(llm): widen query band to 25-30 + industry-specific brainstorm (prompt v5)` (atomic) |
| `6fd2555` | `docs(plan): wider query band implementation plan (2-chunk reviewed)` |
| `d2ace37` | `docs(spec): wider query band (25-30) + industry-specific brainstorm design` |
| —         | *↑ wider query-band slice (2026-05-16, on this branch) · ↓ subreddit-discovery slice (merged to `main` @ `ebe2bc2`)* |
| `e92fd19` | `feat(llm): grounded subreddit discovery — prompt v4 + Wave 0 wiring` (Task 6+7, atomic) |
| `d76bcc3` | `feat(llm): Call #1 prompt + SubredditSearchPhrases schema (v1)` |
| `2451f33` | `feat(sources): /subreddits/search client (spec step 2)` |
| `2f756a1` | `feat(llm): deterministic subreddit pipeline (spec §7)` |
| `7a8dc90` | `feat(sources): SubredditCandidate/PhraseResult DTOs + table render` |
| `aaf59a4` | `refactor(sources): shared process-wide Reddit limiter (spec step 0)` |
| `df5af8e` | `chore(format): apply ruff format to pre-existing drift (baseline)` |
| `4264d38` | `docs(plan): subreddit discovery implementation plan (5-chunk reviewed)` |
| —         | *↑ subreddit-discovery slice (2026-05-16) · ↓ earlier Wave 0 slice* |
| `23a4096` | `feat(cli): call plan_job between create_job and enqueue` |
| `43de2c9` | `feat(orchestrator): Reddit reads from job.job_plan with template fallback` |
| `65d12a4` | `feat(orchestrator): add plan_job (Wave 0 inline, fallback-safe)` |
| `d4196f8` | `feat(llm): add Wave 0 Query Expansion station (gpt-5.4)` |
| `cd6973c` | `feat(orchestrator): add Reddit query validator (skill items 6/7/10/16)` |
| `c270ae9` | `feat(llm): add Wave 0 query expansion prompt module (v1)` |
| `af5b54b` | `feat(llm): add RedditQuerySpec + JobPlan schemas` |
| `226d17e` | `feat(llm): add call_openai (provider-specific, lazy client)` |
| `4c75c71` | `refactor(llm): rename call_llm to call_anthropic (provider split prep)` |
| `36de60c` | `feat(llm): add diskcache wrapper (cache_key/get/put)` |
| `a4bc6b0` | `chore(deps): add openai>=1.50 (Wave 0 query expansion)` |
| `35934f0` | `feat(config): add optional OPENAI_API_KEY setting` |
| `8beff7c` | `feat: discovery run CLI + Wave 1 Reddit orchestrator` |
| `781da99` | `feat: date-anchored Job creation (JobSpec + create_job)` |
| `ce60e55` | `feat: worker bridge — claim, dispatch to source, persist to Bronze` |
| `991e590` | `feat: foundation slice — content hashing, DB schema, Reddit adapter` |
| `d0fc1f5` | `chore: initial project scaffold` |

---

## Pieces that exist (the map)

```
src/discovery/
├── __init__.py                  # __version__
├── hashing.py                   # hash_params() — sha256 of canonical JSON
├── jobs.py                      # JobSpec + create_job (idempotent on spec_hash)
├── cli/
│   ├── main.py                  # typer app, registers `version`, `hello`, `run`
│   ├── init_db.py               # `python -m discovery.cli.init_db` → alembic upgrade head
│   └── run.py                   # `discovery run` — create_job + plan_job + enqueue + drain
├── config/
│   └── settings.py              # pydantic-settings; ANTHROPIC_API_KEY + OPENAI_API_KEY
├── db/
│   ├── __init__.py              # public surface re-exports
│   ├── models.py                # Job, Task, RawRecordRow, PainSignal + UtcDateTime
│   └── engine.py                # async engine factory + session maker
├── llm/
│   ├── client.py                # call_anthropic + call_openai (no facade)
│   ├── cache.py                 # diskcache wrapper — cache_key / get_cached / put_cached
│   ├── schemas.py               # RedditQuerySpec + JobPlan (Wave 0 output)
│   ├── prompts/
│   │   └── query_expansion.py   # VERSION + SYSTEM_PROMPT + FEW_SHOT_EXAMPLES + build_user_message
│   └── stations/
│       └── query_expansion.py   # run_query_expansion(spec) -> JobPlan
├── orchestrator/
│   ├── jobs.py                  # plan_job(session, job) — Wave 0 inline, fallback-safe
│   ├── reddit.py                # template + reads from job.job_plan
│   └── reddit_query_validator.py # pure validator for LLM-built queries
├── sources/
│   ├── base.py                  # BaseSource ABC + RawRecord Pydantic DTO
│   └── reddit.py                # RedditSource (anonymous .json endpoint)
├── workers/
│   ├── __init__.py              # build_default_registry() + public surface
│   └── worker.py                # claim_one, run_one, run_worker_once, sweep_stuck_tasks
├── normalizers/                 # empty — Wave 2 lives here later
└── (no orchestrator/__init__.py for other sources yet)

migrations/versions/eade55a73c8f_initial_schema_*.py   # 4 tables
.claude/skills/
├── llm-station/SKILL.md         # contract for any LLM call site (+ per-station deviation table)
├── source-adapter/SKILL.md      # contract for any new source
└── reddit-source/SKILL.md       # operational rules for Reddit specifically
docs/plans/2026-05-14-wave-0-query-expansion.md  # the slice plan that landed
```

The four DB tables: **`jobs`**, **`tasks`**, **`raw_records`**, **`pain_signals`**.
Later waves will add `companies`, `tools`, `reviews`, `job_postings`,
`tools_mentioned`, `signal_company_links`, etc. — none of those exist yet.

---

## Decisions locked in (don't re-litigate without good reason)

These came out of explicit user sign-off in earlier sessions. Each has
a "why" attached — when in doubt, check the why before changing them.

- **Single-worker assumption.** `tasks.claimed_at` alone drives the
  stuck-sweep. No `worker_id` column. Documented in CLAUDE.md's
  *Architecture rules*. Add `worker_id` only when a second worker
  process is introduced.
- **`RawRecord` DTO ≠ `RawRecordRow` DB row.** Different layers,
  different names. Adapters return DTOs; the worker turns DTOs into
  rows on insert.
- **`raw_records.body` is JSON, not bytes.** Every Wave 1 source
  returns JSON. The source-adapter skill says "raw JSON body". When
  Playwright HTML scrapes land later this widens to `dict | str`.
- **Enums stored as VARCHAR, no SQL CHECK.** StrEnum on the Python
  side, `SAEnum(..., native_enum=False, create_constraint=False)` on
  the SQL side. CHECK constraints fight you on every enum extension.
- **Timezone-aware UTC always.** Via the `UtcDateTime` TypeDecorator
  in `db/models.py`. SQLite drops tzinfo natively; this re-attaches
  it on read.
- **Migrations use `render_item` hook** so they emit `sa.DateTime` and
  `sa.String` instead of `UtcDateTime` / `AutoString`. Every migration
  is self-contained — no `import sqlmodel`, no `import discovery`.
- **`hash_params()` is the one and only content-hash recipe.** sha256
  over canonical JSON (`sort_keys=True`, `separators=(",", ":")`,
  `ensure_ascii=False`). Used by `Task.content_hash`,
  `raw_records.content_hash`, and (when it lands) the LLM diskcache key.
- **`PainSignal.tools_mentioned` and `company_mentions` are
  transitional JSON columns** marked with `# transitional` comments.
  When the dedicated tables land, a migration backfills and drops them.
- **`JobSpec.as_of` is required.** Without it, May's run hashes
  identical to June's and we'd refuse to create a new job.
  Re-running monthly is the whole point.
- **`(source, external_id)` is UNIQUE on `raw_records`.** Popular
  posts seen across monthly re-runs are stored once. Idempotent;
  cheap incremental discovery.
- **Worker uses `expire_on_commit=False` async sessions.** Anything
  else triggers lazy-loads after commit → `MissingGreenlet`. The
  default `async_session_factory()` sets this.
- **One Reddit task bundles all four queries.** `RedditSource.fetch`
  handles partial-success internally. Trade-off: per-query retry
  granularity is lost in exchange for fewer task rows. (Wave 0 now
  produces 25-30 LLM queries, but they still all go in one task.)
- **Two provider functions, no facade.** `call_anthropic` and
  `call_openai` are independent; no generic `call_llm` dispatcher.
  Each function handles its provider's quirks (Anthropic's top-level
  `system=` vs OpenAI's `developer`-role messages entry) and retries
  its own SDK's exception classes.
- **Wave 0 runs inline via `plan_job`, not as a worker task.** A
  conscious "Option A" deviation from the "LLM calls are tasks" rule
  for this one station. See `docs/plans/2026-05-14-wave-0-query-expansion.md`
  for the decision record and the promotion-to-Option-B path.
- **Wave 0 LLM brainstorms; Python validates.** The LLM emits
  complete OR-compressed `q` strings inside `RedditQuerySpec` objects;
  `discovery.orchestrator.reddit_query_validator` enforces the
  skill rules (uppercase operators, URL cap, valid subreddit names,
  endpoint vs subreddit count). Invalid queries are dropped; if too
  few survive, the station raises and `plan_job` falls back to the
  template.
- **Query Expansion uses temperature 0.2, not 0.** Stations deviate
  from the `llm-station` skill's `temperature=0` default when the
  station is brainstorming creative work. Documented in
  `.claude/skills/llm-station/SKILL.md`'s per-station deviation table.
- **`JobPlan` is permissive (`extra="allow"`).** Future prompts can
  emit `youtube_queries` / `news_keywords` / `apollo_params` etc.
  without a code change — but adding a typed field is required before
  reading those in app code. Documented in `discovery.llm.schemas`'s
  module docstring.

---

## Skills are policy, not optional

Three project skills under `.claude/skills/` encode hard contracts.
Read them before touching the relevant code path:

- **`source-adapter`** — every file under `src/discovery/sources/`.
- **`reddit-source`** — anything touching `src/discovery/sources/reddit.py`
  or planning Reddit queries from a `JobPlan`.
- **`llm-station`** — every LLM call site (anything that imports
  `discovery.llm.client.call_anthropic` or `call_openai`).

The user has been clear: these are "the project's policy" on those
topics, not loose guidelines.

---

## What's NOT built yet

- **Wave 2 (pain classification LLM station).** No
  `discovery.llm.schemas.PainExtraction`, no `run_pain_extraction()`.
  This is a candidate for the next slice.
- **The other eleven sources.** Only Reddit. YouTube, HN, Apollo,
  Google Places, Yelp, OpenCorporates, trade directories, NewsAPI,
  Listen Notes, Product Hunt, Census — all unbuilt.
- **Waves 3 & 4 (per-company / per-tool enrichment).** Reviews, job
  postings, tech stack, etc. — none of those tables exist.
- **Wave 5 (link, sanity-check, aggregate).** No cross-linking SQL,
  no rule-based outlier detection, no sanity-check LLM station.
- **A `discovery work` loop CLI.** We have `run_worker_once`; a
  long-running drain loop is trivial but unbuilt.
- **VCR cassette for Reddit.** Tests use `httpx.MockTransport`.
  Recording a real cassette needs network access.

---

## Next slice: open

Wave 0 landed, then was upgraded to **grounded subreddit discovery**
(2026-05-16 slice — see the dedicated section below). The next slice
is the user's call. Candidates, in rough order of payoff:

1. **Wave 2 — Pain Classification LLM station.** Promotes raw Reddit
   posts (Bronze) into `pain_signals` rows (Silver). Anthropic
   Sonnet, batched, follows the same `llm-station` contract Wave 0
   established. Schema: `PainExtraction` model, station
   `run_pain_extraction(batch)`. ~3-4 days of work.

2. **Second source adapter.** YouTube, HN, NewsAPI, or Product Hunt
   are the easiest next picks because they have clean REST APIs and
   match the `source-adapter` skill's shape. With Wave 0 emitting
   richer `JobPlan` fields (`youtube_queries`, `news_keywords`), the
   second source can already consume LLM-built queries.

3. **VCR cassette for Reddit happy path.** Tests currently use
   `httpx.MockTransport`. A real recording would catch upstream
   schema drift.

4. **`discovery work` long-running drain loop.** `run_worker_once`
   exists; turning it into a daemonized loop is trivial.

## Future considerations — promoting Wave 0 to Option B

Wave 0 currently runs inline in `plan_job(session, job)`. The
architecture rule "LLM calls are tasks, not function calls" was
**deliberately deferred** for this one station — see
`docs/plans/2026-05-14-wave-0-query-expansion.md` (the "Decision
record" section). Promote to a worker task when at least one of:

- A second worker process is introduced — parallel job runs would
  benefit from queue-level concurrency.
- A `discovery status` dashboard wants Wave 0 failures visible in
  `tasks` alongside other failures.
- Cumulative serial-orchestration overhead starts showing up in
  measurements (re-measure first; A's overhead is ~50 ms per job).

The promotion path is a ~20-line `wave_0_task` wrapper around
`plan_job`. `run_query_expansion(spec) -> JobPlan` is already
orchestrator-agnostic; no station code changes needed.

---

## Wider query band + industry brainstorm (2026-05-16) — what shipped & locked in

Built from `docs/specs/2026-05-16-wider-query-band-design.md` (approved,
4-pass spec-reviewed) via `docs/plans/2026-05-16-wider-query-band.md`
(2-chunk plan-reviewed). The subreddit-discovery slice (below) is on
`main` (`ebe2bc2`); this slice sits on top, not yet merged.

**Problem:** a 1-month / 12-query run on "wedding photography" returned
only 2 posts — the plan was too narrow (10–15 queries from the generic
8-pain-category grid only).

**What shipped — one atomic commit `1eb7b6d` + a docstring-only
follow-up `7aa7b9c`:**

- `JobPlan.reddit_queries` band **10–15 → 25–30** (`schemas.py`, one
  `Field` line). **This supersedes the prior locked 10–15 decision.**
  The new authority on the query band is
  `docs/specs/2026-05-16-wider-query-band-design.md`. A future session
  must NOT "restore" 10–15 as a regression.
- `query_expansion` prompt **v4 → v5**: all count language → 25–30; a
  new "Two kinds of queries" section — keep the generic pain-grid
  (kind 1, industry-AGNOSTIC) AND additionally brainstorm
  industry-specific queries (kind 2, re-derived for the requested
  industry); a fenced ONE-industry illustration (wedding photography)
  with an explicit "re-derive your own, never copy" guard; the
  "don't make domain-specific phrase lists" generality rule was
  **rescoped to the standard grid only** so the prompt no longer
  self-contradicts; `build_user_message`'s user-turn count string is
  the second 25–30 lever.
- **Scoped reddit-source skill item-9 deviation:** kind-2 deliberately
  bends item-9 (generality); kind-1 still honours it. Intentional,
  user-approved, documented in the spec + prompt.
- **Station logic UNCHANGED.** `MIN_VALID_QUERIES = 10` stays,
  decoupled from the schema floor — pruning never collapses to the
  template unless <10 valid survive; the tail's `model_construct`
  means a pruned set is not re-validated against `min_length=25`. The
  separate commit `7aa7b9c` only corrected the station's now-stale
  docstrings (v4→v5, 10-15→25-30, min_length=10→25) — zero logic change.
- `FEW_SHOT_EXAMPLES` still NOT wired into the LLM call (long-standing
  deferred follow-up) — and now also visibly inconsistent (~10 example
  queries vs the 25–30 band). Cosmetic only (dead data); still deferred.

**Smoke (real gpt-5.4 + Reddit, 2026-05-16):** `discovery run
--industry "wedding photography" --location US --time-window year` →
`wave 0: planned`, `subreddit discovery: 120 candidates survived`,
**28 queries** (8 generic pain-grid + 20 genuinely re-derived
industry-specific: culling, editing backlog/turnaround, client gallery,
contracts, deposits, client ghosting, CRM/booking, second shooter,
"uncle bob"/unplugged ceremony, rain plan, intake forms, album/print
lab, outsourcing editing — well beyond the prompt's 6-item
illustration), 14 subs, **176 posts** stored vs **2** on the earlier
1-month / 12-query run for the same industry. The wider band + the
two-kinds composition is the recall fix; `--time-window` is the other
(unchanged) lever. 231 tests green; `ruff`/`mypy` clean.

## Subreddit-discovery slice (2026-05-16) — what shipped & locked in

Built from `docs/specs/2026-05-15-subreddit-discovery-design.md` via a
5-chunk reviewed plan (`docs/plans/2026-05-16-subreddit-discovery.md`),
8 commits on `main` @ `63beef5`. Every task: TDD → independent
spec-compliance review → independent code-quality review → fix loop.

**Problem solved:** Wave 0 used to ask the LLM to *name* subreddits
from training memory (hallucination + staleness). Now the LLM emits
semantic *search phrases*; Reddit's `/subreddits/search.json` returns
real, currently-existing subreddits; deterministic code ranks them; a
second grounded LLM call picks only from that table and designs the
content queries with the unchanged v3 rules (prompt now v4).

**New pieces (add to the map above):**

- `src/discovery/sources/reddit_ratelimit.py` — process-wide shared
  Reddit `AsyncLimiter` singleton (`get_reddit_limiter`,
  `reset_reddit_limiter`). `RedditSource` defaults to it.
- `src/discovery/sources/reddit_subreddits.py` — `SubredditCandidate`
  / `PhraseResult` DTOs, `clean_description`, `render_candidate_table`
  (the 6-col LLM table), `_SubredditT5` response model, and the async
  `search_subreddits` client (401/403 raise, retry mirror of
  `reddit.py`, partial success, per-request skill-21 log). NOT a
  `BaseSource`; returns planning DTOs, never Bronze.
- `src/discovery/llm/stations/subreddit_selection.py` — the pure
  deterministic pipeline (`dedupe_and_count`, `drop_non_public`,
  `drop_nsfw`, `subscriber_median`, `drop_below_median`,
  `with_activity_ratio`, `reject_off_table`, `trim_overflow`).
- `src/discovery/llm/prompts/subreddit_phrases.py` — Call #1 prompt
  (`VERSION="v1"`). `schemas.py` gained `SubredditSearchPhrases`.
- `src/discovery/llm/prompts/query_expansion.py` — `VERSION` v3→**v4**,
  GROUNDING section added, `build_user_message(spec)` →
  `build_user_message(spec, table)`.
- `src/discovery/llm/stations/query_expansion.py` — `run_query_expansion`
  rewritten into the multi-step flow; signature unchanged.

**Decisions locked in (don't re-litigate):**

- **One shared process-wide Reddit limiter.** Sub-search (Wave 0) and
  content-fetch (Wave 1) share ONE 10/60.1s budget via the
  `reddit_ratelimit` singleton. Never default-construct a second.
- **Wave 0 = two LLM calls + a deterministic middle.** No LLM in the
  ranking. Folded inside the existing inline "Option A" `plan_job`;
  the Option-B promotion path still applies.
- **The LLM in Call #2 may pick ONLY from the supplied table.** Off-
  table picks are deterministically stripped (`reject_off_table`).
  Selection is adaptive, hard ceiling 30, no minimum.
- **One combined Wave 0 cache entry**, key
  `f"{subreddit_phrases.VERSION}+{query_expansion.VERSION}"` passed to
  the *existing* `cache_key` (cache module unchanged). Bumping either
  VERSION re-runs phrase-gen + search + selection.
- **`MIN_VALID_QUERIES=10` and the deterministic tail are UNCHANGED.**
  The `JobPlan.reddit_queries` schema band, HOWEVER, was **superseded**
  by the later wider-query-band slice — it is now `min_length=25,
  max_length=30`, NOT 10–15 (see that dated section above; authority:
  `docs/specs/2026-05-16-wider-query-band-design.md`; do not "restore"
  10–15). The "do not relax the floor" guidance applies ONLY to
  `MIN_VALID_QUERIES` (still 10, decoupled from the schema band so
  pruning never collapses to the template unless <10 valid survive),
  NOT to the schema band. Still do not add a subreddit-count floor; the
  §10 "too few remain" is realized through the existing
  query-validation (`MIN_VALID_QUERIES`) path only.
- **Retry duplication is spec-sanctioned.** `reddit_subreddits.
  _get_with_retries` mirrors `reddit.py._fetch_with_retries` (the
  only divergence: 401/403 must raise, never empty). See follow-ups
  for the deferred DRY extraction.

**Smoke verified (real OpenAI gpt-5.4 + real Reddit), 2026-05-16:**

- Rich — `discovery run --industry "food truck" --location US`:
  `wave 0: planned`, 13 LLM-authored queries, real posts (r/austinfood,
  r/foodtrucks).
- Niche — `--industry "mobile dog grooming" --location US
  --time-window year`: `wave 0: planned`, `subreddit discovery: 71
  candidates survived (median subs=91957.5)`, 13 queries, 6 posts, 5
  per-phrase skill-21 `subreddit search done` log lines. Confirms a
  niche table still produces 10–15 queries and does NOT auto-fallback.
- Cache re-hit (same niche spec): ~4 s, `wave 0: planned`, **0**
  sub-search lines — the combined cache entry skips phrase-gen +
  search + selection in one shot (spec §8).

No `fallback` / `QueryExpansionError` in any run. 229 unit tests green;
`test_orchestrator_jobs.py` untouched & green (it stubs
`run_query_expansion(spec)->JobPlan`; signature unchanged).

## How to verify the project is healthy when you resume

```bash
$ uv sync                              # install deps
$ uv run pytest                        # expect: 135 passed
$ uv run ruff check .                  # expect: All checks passed!
$ uv run ruff format --check .         # expect: all files formatted
$ uv run mypy src/                     # expect: Success: no issues found
$ uv run discovery --help              # expect: version, hello, run subcommands
```

If any fail, fix before starting the next slice.

---

## Post-slice fixes (2026-05-15, after the initial Wave 0 commit)

The slice was declared done at commit `e8fb355`. Live-testing
immediately after surfaced three real issues; all fixed.

- `ca0b116 fix(config): share .env across worktrees; isolate database_url`
  — `PROJECT_ROOT` now walks up past `.claude/worktrees/<name>/` to
  the main project root, so every worktree reads the same `.env` file
  instead of needing its own copy of secrets. The main project happens
  to define `DATABASE_URL` for a different app (Postgres), so
  `Settings.database_url` is now bound via `validation_alias` to
  `DISCOVERY_DATABASE_URL` only — the bare `DATABASE_URL` is ignored.
- `e6692fd fix(llm): rename max_tokens → max_completion_tokens for gpt-5.x`
  — OpenAI rejected `max_tokens` at runtime for gpt-5.4 with a 400
  asking for `max_completion_tokens`. `call_openai` now translates at
  the boundary; callers keep using `max_tokens=...` for parity with
  `call_anthropic`. The two gpt-5.x parameter renames (`system →
  developer`, `max_tokens → max_completion_tokens`) both live in
  `call_openai` and are unit-tested.
- `4610a88 fix(llm): add subreddit field for per_sub queries (prompt v2)`
  — `RedditSource.fetch` requires a `subreddit` key on per_sub queries;
  the v1 prompt told the LLM "the subreddit is implied by the endpoint"
  but gave it no structured field to set it in. Schema now has
  `RedditQuerySpec.subreddit: str | None` enforced by `model_validator`
  (required on per_sub, forbidden on site_wide). Validator checks the
  name format. Compiler passes it through. Prompt VERSION → v2.

**End-to-end verified after the fixes:** `discovery run --industry
"food truck" --location US` → gpt-5.4 produced 13 valid queries →
Reddit pulled 11 posts into `raw_records`. Total branch state: 16
commits ahead of `main`, 148 unit tests green, lint + format + mypy
clean.

---

## Open follow-ups (smaller, not blocking the next slice)

- **DRY the Reddit retry policy.** `reddit.py._fetch_with_retries` and
  `reddit_subreddits._get_with_retries` duplicate the skill-item-4
  policy (intentional, spec-sanctioned mirror — extraction was out of
  step-0 scope). Extract a shared `reddit_http` retry helper when
  convenient; the only behavioral difference to preserve is sub-search
  401/403 → raise (never empty).
- **`FEW_SHOT_EXAMPLES` is never sent to the LLM.** Pre-existing (true
  in pre-feature Wave 0 too): `query_expansion.py` defines
  `FEW_SHOT_EXAMPLES` but `call_openai` only takes `system`+`user`, and
  neither `SYSTEM_PROMPT` nor `build_user_message` injects it. Either
  serialize it into the prompt or rename it `_REFERENCE_EXAMPLES` and
  retarget its shape test. Orthogonal to subreddit discovery; needs its
  own behavior-validation if wired in.
- **Tunables to revisit with real Item-21 data (spec §13):** the
  `DRASTIC_FLOOR_DIVISOR = 10` median divisor and the ~5 phrase count
  (prompt-tunable, no code change). The niche smoke showed median
  subs ≈ 92k for "mobile dog grooming" — gather more distributions
  before tuning.
- `run_worker_loop` + `discovery work` CLI command (drain queue
  continuously). Trivial.
- VCR cassette for the Reddit happy-path test (requires recording
  against real Reddit once).
- Closing adapter HTTP clients on worker shutdown. `RedditSource` has
  `aclose()`; `build_default_registry()` doesn't call it.
- `pyproject.toml` formatting — `uv add` reflowed the dependency list
  and collapsed blank-line separators between sections. Cosmetic only;
  restore when convenient.
- The `call_anthropic` default model is still `claude-sonnet-4-5`.
  After Wave 0's provider split landed, the default works as expected
  for any future Anthropic station; verify when adding the next one.

---

## Sources for the GPT-5.4 model details I cited above

- [Introducing GPT-5.4 | OpenAI](https://openai.com/index/introducing-gpt-5-4/)
- [GPT-5.4 — Wikipedia](https://en.wikipedia.org/wiki/GPT-5.4)
- [Introducing GPT-5.4 mini and nano | OpenAI](https://openai.com/index/introducing-gpt-5-4-mini-and-nano/)
- [OpenAI launches GPT-5.4 with Pro and Thinking versions — TechCrunch](https://techcrunch.com/2026/03/05/openai-launches-gpt-5-4-with-pro-and-thinking-versions/)
