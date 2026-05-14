---
name: plan
description: Enter explore-plan mode before a non-trivial change
---

I want to make a non-trivial change: **$ARGUMENTS**

Follow this workflow strictly:

**Phase 1 — Explore.** Do not write code yet. Read the relevant files
(use `@` references). If the topic touches the pipeline, read
`docs/architecture.md` first. Use a subagent if you'd need to read more
than 5 files. Report what you found in 5-10 lines.

**Phase 2 — Open questions.** If there is any architectural choice with
2+ reasonable answers, use the AskUserQuestion tool to surface it. Wait
for my answers before continuing.

**Phase 3 — Plan.** Write a numbered plan to `plans/<short-name>.md`. The
plan should include:

- What files change (with paths)
- What new files get created (with paths)
- What tests get added or changed
- Any open risks I should know about

Show me the path to the plan file. Wait for my approval to proceed.

**Phase 4 — Implement.** Only after I say "go" or "proceed", execute the
plan. After implementation, run `/run-checks`. If anything fails, fix it
once. If it fails again, stop and report.

**Phase 5 — Summarize.** Tell me in plain English what changed and what
I should review before committing.
