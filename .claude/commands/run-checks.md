---
name: run-checks
description: Run lint, type check, and tests in order. Stop on first failure.
---

Run the project's full quality gate, in order, stopping at the first
failure. Report each step's outcome clearly.

1. `uv run ruff check --fix .`
2. `uv run ruff format --check .`   (NOT `--fix`; we just want to know
   if formatting drifted)
3. `uv run mypy src/`
4. `uv run pytest -x --tb=short`

After each step:

- If it passes, say "✓ <step name>" and continue.
- If it fails, stop. Show me the failing output. Do NOT try to fix it
  unless I explicitly ask. Just surface what broke.

At the end, if all four pass, say "All checks green." in plain English.
Do not list anything else.
