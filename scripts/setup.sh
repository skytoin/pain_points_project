#!/usr/bin/env bash
# scripts/setup.sh
#
# One-shot setup. Idempotent — safe to re-run.
#
# Usage:  bash scripts/setup.sh
# Optional:
#   GITHUB_REPO=discovery   # repo name to create
#   PRIVATE_REPO=1          # 1=private (default), 0=public

set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-discovery}"
PRIVATE_FLAG="--private"
[[ "${PRIVATE_REPO:-1}" == "0" ]] && PRIVATE_FLAG="--public"

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }
warn() { printf "\n\033[1;33m! %s\033[0m\n" "$*"; }
fail() { printf "\n\033[1;31m✗ %s\033[0m\n" "$*"; exit 1; }

# --- 1. uv ----------------------------------------------------------
say "Step 1/8 — checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  warn "uv not found. Installing…"
  if [[ "$(uname)" == "Darwin" || "$(uname)" == "Linux" ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1090
    [[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env" || true
    export PATH="$HOME/.local/bin:$PATH"
  else
    fail "Auto-install only supported on macOS/Linux. See https://docs.astral.sh/uv/"
  fi
fi
uv --version

# --- 2. Sync deps ---------------------------------------------------
say "Step 2/8 — installing dependencies (this is the slow one, ~30s)"
uv sync --dev

# --- 3. Playwright browsers ----------------------------------------
say "Step 3/8 — installing Chromium for Playwright"
uv run playwright install chromium

# --- 4. .env --------------------------------------------------------
say "Step 4/8 — preparing .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  warn "Created .env from template. EDIT IT and add real API keys before running the pipeline."
else
  say "  .env already exists — leaving it alone"
fi

# --- 5. Personal Claude Code settings ------------------------------
say "Step 5/8 — preparing .claude/settings.local.json"
if [[ ! -f .claude/settings.local.json ]]; then
  cp .claude/settings.local.json.example .claude/settings.local.json
  say "  Created .claude/settings.local.json (gitignored)"
fi

# --- 6. Data dir + DB init -----------------------------------------
say "Step 6/8 — creating data/ and initializing DB stub"
mkdir -p data
uv run python -m discovery.cli.init_db || warn "init_db stub returned non-zero (OK for now)"

# --- 7. Smoke tests ------------------------------------------------
say "Step 7/8 — running smoke tests"
uv run pytest tests/unit/test_smoke.py -v

# --- 8. Git + GitHub -----------------------------------------------
say "Step 8/8 — git + GitHub"
if [[ ! -d .git ]]; then
  git init -q
  git add .
  git commit -q -m "chore: initial project scaffold"
  say "  Initialized git repo and made first commit"
else
  say "  Git repo already initialized — skipping"
fi

if command -v gh >/dev/null 2>&1; then
  if ! gh repo view "$GITHUB_REPO" >/dev/null 2>&1; then
    if gh auth status >/dev/null 2>&1; then
      say "  Creating private GitHub repo '$GITHUB_REPO' and pushing"
      gh repo create "$GITHUB_REPO" "$PRIVATE_FLAG" --source=. --remote=origin --push
    else
      warn "Run \`gh auth login\` first, then re-run this script to create the repo."
    fi
  else
    say "  Repo '$GITHUB_REPO' already exists on GitHub — skipping create"
  fi
else
  warn "GitHub CLI (gh) not installed. Skipping repo creation."
  warn "Install: https://cli.github.com/  then run \`gh auth login\` and re-run this script."
fi

cat <<DONE

\033[1;32m✓ Setup complete.\033[0m

Next steps:
  1. Open .env and fill in your API keys (at minimum ANTHROPIC_API_KEY).
  2. Run \`claude\` from this directory.
  3. First message: paste this:

     Read CLAUDE.md and docs/architecture.md, then summarize the project
     back to me and list any questions you have. Don't write code yet.

DONE
