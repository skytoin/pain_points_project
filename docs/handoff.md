# Session handoff — discovery pipeline

**Last touched:** 2026-05-14
**Branch:** `claude/elated-haslett-f7d26f` (4 commits ahead of `main`, pushed
to `origin` as of last session if you ran the `git push` command)

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
queued task: 1  (source=reddit, queries=4)
  ✓ processed task 1
done. 1 task(s) processed.
```

After running, `data/discovery.db`'s `raw_records` table holds real
Reddit posts. The pipeline goes:
**JobSpec → create_job → enqueue_reddit_task_for_job → run_worker_once
→ RedditSource.fetch → raw_records rows.**

**Test counts:** 83 unit tests, all green. `ruff check`, `ruff format`,
`mypy --strict`, and `pytest` all pass.

---

## Commit history (newest first)

| SHA       | Slice |
|-----------|---|
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
│   └── run.py                   # `discovery run` — create_job + enqueue + drain
├── config/
│   └── settings.py              # pydantic-settings; reads ANTHROPIC_API_KEY etc.
├── db/
│   ├── __init__.py              # public surface re-exports
│   ├── models.py                # Job, Task, RawRecordRow, PainSignal + UtcDateTime
│   └── engine.py                # async engine factory + session maker
├── llm/
│   └── client.py                # call_llm() — currently Anthropic-only via instructor
├── orchestrator/
│   └── reddit.py                # hand-rolled query template + enqueue helper
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
├── llm-station/SKILL.md         # contract for any LLM call site
├── source-adapter/SKILL.md      # contract for any new source
└── reddit-source/SKILL.md       # operational rules for Reddit specifically
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
  granularity is lost in exchange for fewer task rows.

---

## Skills are policy, not optional

Three project skills under `.claude/skills/` encode hard contracts.
Read them before touching the relevant code path:

- **`source-adapter`** — every file under `src/discovery/sources/`.
- **`reddit-source`** — anything touching `src/discovery/sources/reddit.py`
  or planning Reddit queries from a `JobPlan`.
- **`llm-station`** — every LLM call site (anything that imports
  `discovery.llm.client.call_llm`).

The user has been clear: these are "the project's policy" on those
topics, not loose guidelines.

---

## What's NOT built yet

- **Wave 0 (LLM query expansion).** The orchestrator currently uses a
  hand-rolled template; the LLM station that turns a fuzzy spec into
  source-specific params doesn't exist. **This is the next slice.**
- **Wave 2 (pain classification LLM station).** No
  `discovery.llm.schemas.PainExtraction`, no `run_pain_extraction()`.
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

## Next slice: Wave 0 — LLM Query Expansion (user-requested)

**The user explicitly chose, after seeing the alternative:**

- **Model: `gpt-5.4`** (OpenAI, released March 5, 2026 — has Thinking,
  Pro, mini, and nano variants; main `gpt-5.4` is the default unless
  they pick one).
- **Provider: OpenAI.** Not Anthropic. The user has an OpenAI API key
  already and explicitly wants to switch *this station* to OpenAI.
- **Output: ≥10 queries** (user said "10 for example"). Hand-rolled
  template currently produces 4.

This is the first multi-provider station in the project. Path B from
the previous session's discussion: refactor `call_llm` to handle both
providers instead of having two separate clients.

### What this slice has to build

1. **Add `openai` dep.** Propose in `pyproject.toml` — CLAUDE.md says
   the user applies it.
2. **Add `OPENAI_API_KEY` to `config/settings.py`** as a `SecretStr`
   (not optional — required when any OpenAI station is in play).
3. **Refactor `src/discovery/llm/client.py`** to dispatch by provider:
   - Add a `provider` arg or split into two clients with a shared
     interface.
   - Keep backward-compat for existing Claude callers.
   - `instructor.from_openai(openai.AsyncOpenAI(...))` is the OpenAI
     side; the structured-output contract is the same.
4. **Create the Query Expansion station.** Per the `llm-station`
   skill's eight-step contract:
   - `src/discovery/llm/schemas.py` (new) — `JobPlan` Pydantic model
     with `reddit_queries: list[RedditQuerySpec] = Field(min_length=10, max_length=15)`,
     `youtube_queries`, `news_keywords`, `reddit_subreddits` (Wave 0
     should pick domain-specific subs the user-facing template can't),
     etc. Match the architecture doc's `JobPlan` shape.
   - `src/discovery/llm/prompts/query_expansion.py` (new) — `VERSION`,
     `SYSTEM_PROMPT`, `FEW_SHOT_EXAMPLES`, `build_user_message()`.
   - `src/discovery/llm/stations.py` (or `query_expansion.py`) —
     `run_query_expansion(spec: JobSpec) -> JobPlan` with:
     - Cache key via `hash_params({"spec": ..., "prompt_version": ...,
       "model": "gpt-5.4"})`
     - `diskcache` lookup before the LLM call
     - Pydantic validation on the response (instructor handles this)
     - Cache write on success
5. **Wire Wave 0 into the orchestrator.** Replace the hand-rolled
   `reddit_queries_for_spec` call inside `enqueue_reddit_task_for_job`
   with a path that:
   - Calls `run_query_expansion(spec)` to get a `JobPlan`
   - Stores the `JobPlan` JSON onto `Job.job_plan` (column already
     exists in the schema, currently always null)
   - Builds Reddit queries from `JobPlan.reddit_queries` instead of the
     template
   - **Keeps the template as a deterministic fallback** when the LLM
     call fails (per the architecture doc).
6. **Tests.** Mock the OpenAI call via `instructor`'s testing patterns
   or via the diskcache (pre-populate, never hit the LLM). Verify:
   - Cache hits don't fire the LLM
   - Cache key changes when prompt VERSION bumps
   - JobPlan validates as expected (≥10 queries, etc.)
   - Fallback path is taken when LLM raises
   - `Job.job_plan` is populated on success

### Things to think about before coding

- **The `llm-station` skill says default model is Sonnet.** Update the
  skill to clarify that stations can pick their provider/model
  individually — or leave it alone and just deviate for this station.
  User's call.
- **The `claude-api` skill targets Anthropic-only code.** Doesn't apply
  to the OpenAI Wave 0 station, but does apply to the other planned
  stations. No action needed but worth knowing.
- **Cost knob.** `gpt-5.4` has Thinking and Pro variants for higher
  accuracy. For mass classification stations later (Pain, Job-Task),
  `gpt-5.4 mini` or `nano` is cheaper. Worth deciding station-by-station.

---

## How to verify the project is healthy when you resume

```bash
$ uv sync                              # install deps
$ uv run pytest                        # expect: 83 passed
$ uv run ruff check .                  # expect: All checks passed!
$ uv run ruff format --check .         # expect: 38 files already formatted
$ uv run mypy src/                     # expect: Success: no issues found
$ uv run discovery --help              # expect: version, hello, run subcommands
```

If any fail, fix before starting Wave 0.

---

## Open follow-ups (smaller, not blocking Wave 0)

- `run_worker_loop` + `discovery work` CLI command (drain queue
  continuously). Trivial.
- VCR cassette for the Reddit happy-path test (requires recording
  against real Reddit once).
- Closing adapter HTTP clients on worker shutdown. `RedditSource` has
  `aclose()`; `build_default_registry()` doesn't call it.
- The pre-existing `instructor.from_anthropic` setup uses
  `claude-sonnet-4-5` as the default model. After Wave 0 refactors
  `call_llm`, double-check that the default still works for future
  Anthropic stations.

---

## Sources for the GPT-5.4 model details I cited above

- [Introducing GPT-5.4 | OpenAI](https://openai.com/index/introducing-gpt-5-4/)
- [GPT-5.4 — Wikipedia](https://en.wikipedia.org/wiki/GPT-5.4)
- [Introducing GPT-5.4 mini and nano | OpenAI](https://openai.com/index/introducing-gpt-5-4-mini-and-nano/)
- [OpenAI launches GPT-5.4 with Pro and Thinking versions — TechCrunch](https://techcrunch.com/2026/03/05/openai-launches-gpt-5-4-with-pro-and-thinking-versions/)
