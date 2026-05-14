#!/usr/bin/env bash
# .claude/hooks/post-stop-check.sh
#
# Runs once at the end of each Claude Code response (Stop event).
# Designed to be FAST and QUIET — silent on green, brief on red.
#
# It does two things:
#   1. Lints with ruff (no autofix, just signal)
#   2. Enforces project-specific size limits:
#        - files     ≤ 600 lines
#        - functions ≤ 60  lines (Python only)
#
# Exits 0 always (we never want to block Claude's response).

set -u

# Skip in non-project shells
[[ -f pyproject.toml ]] || exit 0

# --- 1. Ruff (quiet on green) ---------------------------------------
if command -v uv >/dev/null 2>&1; then
  ruff_out=$(uv run --quiet ruff check . --no-fix 2>&1 || true)
  if echo "$ruff_out" | grep -qE 'error|warning'; then
    issue_count=$(echo "$ruff_out" | grep -cE 'error|warning' || true)
    if [[ "${issue_count}" -gt 0 ]]; then
      echo "── ruff: ${issue_count} issue(s) ────────────────────────────"
      echo "$ruff_out" | head -15
      echo "  (run \`uv run ruff check --fix .\` to auto-fix many of these)"
    fi
  fi
fi

# --- 2. File-size and function-size limits --------------------------
# Only scans tracked-or-modified Python files; doesn't fan out.

python3 - <<'PY' 2>/dev/null || true
import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_LINES = 600
MAX_FUNC_LINES = 60

# Find recently-modified or staged Python files. Falls back to a quick
# glob if not in a git repo.
try:
    out = subprocess.check_output(
        ["git", "diff", "--name-only", "HEAD"],
        text=True, stderr=subprocess.DEVNULL,
    ) + subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"],
        text=True, stderr=subprocess.DEVNULL,
    )
    candidates = {p for p in out.splitlines() if p.endswith(".py")}
except Exception:
    candidates = {str(p) for p in Path("src").rglob("*.py")}

problems: list[str] = []
func_re = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)\s*\(")

for path in candidates:
    f = Path(path)
    if not f.exists():
        continue
    lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(lines) > MAX_FILE_LINES:
        problems.append(
            f"  {f}: {len(lines)} lines (limit {MAX_FILE_LINES})"
        )

    # Function-length check (naive but cheap: counts lines until dedent)
    for i, line in enumerate(lines):
        m = func_re.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        end = i
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if s.strip() == "":
                continue
            cur_indent = len(s) - len(s.lstrip())
            if cur_indent <= indent and s.strip() != "":
                break
            end = j
        func_len = end - i + 1
        if func_len > MAX_FUNC_LINES:
            problems.append(
                f"  {f}:{i+1} `{m.group(3)}` is {func_len} lines "
                f"(limit {MAX_FUNC_LINES})"
            )

if problems:
    print("── size limits exceeded ──────────────────────────────────")
    for p in problems[:10]:
        print(p)
    if len(problems) > 10:
        print(f"  ...and {len(problems) - 10} more")
    print("  Split into smaller pieces before continuing.")

PY

exit 0
