# discovery

A pipeline that takes a fuzzy industry spec and finds the companies in it,
the tools they use, the pain points they have, and the work patterns inside
their job postings. Five waves, four LLM "stations," everything else is
pure-Python plumbing.

See **`docs/architecture.md`** for the full pipeline walkthrough.

---

## Setup — first time, ~5 minutes

You need [`uv`](https://docs.astral.sh/uv/) installed once on your machine.
`uv` is a fast Python package manager and virtual-environment tool.

### 1. Install uv (one-time, machine-wide)

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Get the project ready

From this folder, in order:

```bash
# Lock + install all dependencies into a local .venv (auto-created)
uv sync --dev

# Install the headless browsers Playwright uses (one-time)
uv run playwright install chromium

# Copy the env template and fill in your API keys
cp .env.example .env
# then open .env in your editor and add real values

# Make the .claude personal-overrides file (optional)
cp .claude/settings.local.json.example .claude/settings.local.json

# Make the local DB folder
mkdir -p data

# Initialize the database (creates tables based on src/discovery/db/models.py)
uv run python -m discovery.cli.init_db
```

### 3. Run the smoke tests

```bash
uv run pytest
```

If everything's green you're good.

---

## Wire it up to GitHub

Run these once, from the project root. They assume you have the
[GitHub CLI (`gh`)](https://cli.github.com/) installed and logged in
(`gh auth login`).

```bash
# Make this a git repo
git init
git add .
git commit -m "chore: initial project scaffold"

# Create a private GitHub repo named "discovery", set it as origin, push
gh repo create discovery --private --source=. --remote=origin --push

# Set your default branch protection (optional but recommended)
gh api repos/:owner/discovery -X PATCH -f default_branch=main
```

If you don't use `gh`, do it by hand:

1. Go to <https://github.com/new>, create a repo named `discovery`,
   leave it empty.
2. Copy the SSH URL it shows you.
3. Run:

   ```bash
   git init
   git add .
   git commit -m "chore: initial project scaffold"
   git branch -M main
   git remote add origin <the-ssh-url-from-github>
   git push -u origin main
   ```

> **Note:** Claude Code is configured to NOT run `git push` for you (that's
> a deny rule in `.claude/settings.json`). You always push manually after
> reviewing the diff. This is intentional — it's your safety net.

---

## Open Claude Code

```bash
claude
```

That's it. The `.claude/settings.json` is set to **acceptEdits** mode,
which means Claude can edit files freely but will still ask before running
shell commands that aren't already on the allowlist. Press `Shift+Tab` once
during a session to cycle the permission mode if you want stricter
(`default`) or looser (`auto`, requires Max/Team/Enterprise plan).

The very first time you open Claude Code in this project, paste:

```
Read CLAUDE.md and docs/architecture.md, then summarize the project back to
me in your own words and list any questions you have. Don't write code yet.
```

This confirms Claude has the right mental model before any work starts.

---

## Common commands

| What you want | Command |
| --- | --- |
| Run all tests | `uv run pytest` |
| Run one test | `uv run pytest tests/unit/test_x.py::test_y -v` |
| Run with coverage | `uv run pytest --cov=discovery` |
| Lint + autofix | `uv run ruff check --fix . && uv run ruff format .` |
| Type check | `uv run mypy src/` |
| All checks at once | `/run-checks` (inside Claude Code) |
| Add a new dependency | `uv add <package>` |
| Add a dev dependency | `uv add --dev <package>` |
| Update lockfile | `uv lock --upgrade` |
| Create a migration | `uv run alembic revision --autogenerate -m "<message>"` |
| Apply migrations | `uv run alembic upgrade head` |

---

## Layout

```
discovery/
├── CLAUDE.md                  ← project memory; Claude reads every session
├── pyproject.toml             ← deps + ruff + mypy + pytest config
├── README.md                  ← you are here
├── .env.example               ← copy to .env, add API keys
├── .gitignore
├── .claude/
│   ├── settings.json          ← shared permission config (checked in)
│   ├── settings.local.json    ← personal overrides (gitignored)
│   ├── commands/              ← /run-checks, /new-source, etc.
│   ├── skills/                ← on-demand domain knowledge
│   └── hooks/                 ← scripts run at lifecycle events
├── docs/
│   └── architecture.md        ← the pipeline plan
├── src/discovery/
│   ├── config/                ← pydantic-settings
│   ├── db/                    ← SQLModel models, alembic
│   ├── sources/               ← one file per external API
│   ├── llm/                   ← anthropic+instructor wrapper, prompts
│   ├── normalizers/           ← Bronze → Silver transforms
│   ├── workers/               ← async worker loops
│   ├── orchestrator/          ← wave runner, planner, barriers
│   └── cli/                   ← typer CLI entrypoints
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/              ← vcr cassettes (recorded HTTP)
```

---

## How Claude Code is set up

A few things to know about this repo so you understand why Claude feels
fast and well-behaved here:

- **`CLAUDE.md` is the rules file.** Kept tight (~150 lines) so it doesn't
  bloat context every turn. It tells Claude your code style, your stack,
  what to ask about before doing.
- **Allowed shell commands are pre-approved.** Anything starting with
  `uv`, `pytest`, `ruff`, `mypy`, `git status`/`diff`/`add`/`commit` runs
  without prompting. Look in `.claude/settings.json`.
- **Push and pyproject.toml edits are blocked.** Deny rules force Claude
  to surface those changes to you.
- **Tests are the verification step.** After any non-trivial change,
  Claude runs `pytest`. If something fails, Claude sees the failure and
  fixes it before claiming done.
- **A Stop hook runs `ruff check`** at the end of each turn (lightweight,
  no auto-fix dump into context).
- **Custom slash commands** like `/run-checks`, `/new-source`,
  `/new-llm-station`, `/plan` live in `.claude/commands/`.

If Claude starts feeling slow:

1. `/context` — see what's loaded
2. `/clear` — start fresh between unrelated tasks
3. Audit `CLAUDE.md` length
4. Audit connected MCP servers (`claude mcp list`)

See `docs/architecture.md` for the pipeline. See `CLAUDE.md` for the rules
Claude follows when working on this codebase.
