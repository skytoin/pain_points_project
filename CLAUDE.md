# Project: discovery

A data discovery pipeline that finds companies, pain signals, tools, and
job-task patterns across an industry. Five waves of work; four LLM stations
plus one inline LLM call inside a worker model. See `docs/architecture.md`
for the full pipeline. Read it once at the start of any non-trivial task.

## How to talk to me

- Write like you are explaining to a curious 14-year-old who is sharp but
  new to the topic. Short sentences. Concrete examples.
- When you use a technical term for the first time in a response, give a
  one-sentence plain-English definition right after it. Example: "We will
  use an event loop (the scheduler inside Python that decides which task to
  run next when one is waiting for I/O)."
- Prefer prose over big bullet lists. Bullets only when there are 3+ truly
  parallel items.
- No emoji. No corporate filler ("Certainly!", "Great question!"). Get to
  the point.

## Ask, don't guess

- When there is a real architectural choice (which library, which storage
  shape, where to put a file) and you can see two or more reasonable
  options, **use the AskUserQuestion tool** to surface the choice before
  coding. Show me 2–4 short options.
- Do NOT ask about routine choices (variable names, obvious imports).
- If a choice is small but you are unsure, state your assumption inline and
  proceed: "I am assuming X; tell me if not."

## Stack

- Python 3.12, managed by **uv** (Rust-based package manager — faster
  pip + virtualenv + pyenv rolled into one).
- Test: pytest, pytest-asyncio, pytest-vcr
- Lint + format: ruff (one tool, replaces black/isort/flake8)
- Types: mypy in strict mode
- HTTP: httpx (async)
- Retry: tenacity. Rate-limit: aiolimiter.
- DB: SQLite via SQLModel (Pydantic + SQLAlchemy in one). Migrations: alembic.
- LLM: anthropic SDK wrapped by `instructor` for Pydantic-validated outputs.
- CLI: typer + rich. Logging: loguru.

## Commands (always use these, never call tools bare)

- Install / sync deps:  `uv sync`
- Add a dep:            `uv add <pkg>`        (NEVER `pip install`)
- Add a dev dep:        `uv add --dev <pkg>`
- Run a script:         `uv run python -m discovery.cli.<name>`
- Run all tests:        `uv run pytest`
- Run one test:         `uv run pytest tests/unit/test_x.py::test_y -v`
- Lint + autofix:       `uv run ruff check --fix . && uv run ruff format .`
- Type check:           `uv run mypy src/`
- Migrate DB:           `uv run alembic upgrade head`
- All quality checks:   `/run-checks`        (custom slash command)

## Code style — hard rules

- **Files: max 600 lines.** If a file passes 500 lines, propose a split.
- **Functions: max 60 lines.** If a function passes 50 lines, propose a
  split into named helpers.
- **One job per function.** If you have to use "and" to describe what a
  function does, it's two functions.
- Type hints on every function signature and every class attribute.
- Public names: descriptive. `fetch_reddit_posts`, not `f1` or `proc`.
- Use `pathlib.Path`, not `os.path`. Use `httpx`, not `requests`. Use
  `loguru.logger`, not `print`.
- Raise specific exceptions. Never bare `except:`. Catch the narrowest
  exception class that fits.
- Async: prefer `asyncio.TaskGroup` over `asyncio.gather` for new code
  (better error semantics in Python 3.11+).

## Architecture rules

- **LLM calls are tasks, not function calls.** They flow through the same
  queue, retry policy, and rate-limiter as any HTTP task. See
  `src/discovery/workers/llm_worker.py`.
- **Every LLM output is Pydantic-validated.** No raw string parsing. If
  the LLM returns invalid output, the task fails cleanly and retries.
- **Cache every LLM call.** Key = content hash + prompt version + model.
  Same input never costs twice.
- **Deterministic first, LLM second.** Use rapidfuzz / SQL / regex to
  handle the easy 80–90%. The LLM only sees the hard remainder.
- **LLM-extracted entities never auto-create canonical rows.** New tool
  names go to a `tools_unverified` queue, not into `tools`.
- **Prompt versions live in code.** Every prompt file exports a `VERSION`
  constant. The cache key includes it.

## Workflow

- For any change touching >1 file or >50 lines: enter plan mode first
  (Shift+Tab), explore the relevant files, propose a plan, then execute.
- After any non-trivial change, run `/run-checks` before declaring done.
- One task per session. Run `/clear` between unrelated tasks.
- Use subagents for investigations that would read >5 files.

## Don't touch without asking

- `pyproject.toml` — propose changes, I will apply them
- `.claude/` — config that affects every future session
- Any file under `migrations/versions/` once committed (DB history)
- `.github/` workflows
- Secrets, `.env`, anything matching `*.key` or `*.pem`

## Gotchas (things that have gone wrong before)

- `python foo.py` runs the *system* Python and misses the venv. Always
  prefix with `uv run`: `uv run python foo.py`.
- `httpx`'s default client is sync. For async use `httpx.AsyncClient`.
- `instructor.from_anthropic(client)` — note `from_anthropic`, not just
  `instructor.patch()` which is the old OpenAI-only API.
- SQLite + `asyncio`: use `aiosqlite` driver via SQLAlchemy's async
  engine, not the blocking stdlib `sqlite3`.

## Memory aids

- Architecture overview: see `docs/architecture.md`
- Per-pattern guides: see `.claude/skills/` (loaded on demand, not every
  session)
