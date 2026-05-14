---
name: new-source
description: Scaffold a new external source adapter following the project conventions
---

I want to add a new source adapter named `$ARGUMENTS`. Use the AskUserQuestion
tool ONCE to ask me these things at the same time, then proceed:

1. What kind of API: REST/JSON, GraphQL, RSS, scrape, or SDK-only?
2. Auth: none, API key, OAuth, or bearer token?
3. Rate limit (requests per second the API allows)?

After I answer, do the following without further prompting:

**Step 1 — Adapter file.** Create `src/discovery/sources/$ARGUMENTS.py`
with:

- A `class $ArgumentsSource(BaseSource)` that subclasses
  `discovery.sources.base.BaseSource`
- One `async def fetch(self, params: ...) -> list[RawRecord]` method
- An `aiolimiter.AsyncLimiter` for rate limiting
- A `tenacity.retry` decorator on the network call
- Type hints on every public name

Reference `src/discovery/sources/base.py` for the contract. Keep the file
under 200 lines.

**Step 2 — Register.** Add the new source to the registry in
`src/discovery/sources/__init__.py`.

**Step 3 — Tests.** Create
`tests/unit/sources/test_$ARGUMENTS.py` with one happy-path test using
`pytest-vcr`. The cassette file goes under
`tests/fixtures/_recorded/$ARGUMENTS_*.yaml`.

**Step 4 — Env var.** Add the API key placeholder to `.env.example`.
(Do NOT edit `.env` — only the example.)

**Step 5 — Run.** Execute `/run-checks` and report the result.

If anything is unclear partway through, stop and ask. Do not invent
endpoint URLs or response shapes — if you don't know the exact API,
say so and ask me for the docs URL.
