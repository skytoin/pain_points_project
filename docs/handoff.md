# Session handoff — discovery pipeline

**Last touched:** 2026-05-15
**Branch:** `claude/quirky-mcclintock-17ee22` (Wave 0 slice — 13 commits
ahead of `main` after the slice landed)

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
Reddit posts. The pipeline goes:
**JobSpec → create_job → plan_job (Wave 0, gpt-5.4) → enqueue_reddit_task_for_job
→ run_worker_once → RedditSource.fetch → raw_records rows.**

When `OPENAI_API_KEY` is unset (or the LLM call fails / validation
drops too many queries), `wave 0` prints `fallback` and the Reddit
orchestrator uses the deterministic hand-rolled template — same as
before Wave 0 shipped. End-to-end behavior is identical to the
pre-Wave-0 run; only the query count and quality change.

**Test counts:** 135 unit tests, all green. `ruff check`, `ruff format`,
`mypy --strict`, and `pytest` all pass.

---

## Commit history (newest first)

| SHA       | Slice |
|-----------|---|
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
  produces 10-15 LLM queries, but they still all go in one task.)
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

Wave 0 landed. The next slice is the user's call. Candidates, in
rough order of payoff:

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
