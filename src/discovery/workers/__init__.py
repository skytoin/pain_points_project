"""Worker pool — claims tasks, dispatches to adapters, persists to Bronze.

Single-worker today per CLAUDE.md's architecture rules. Public surface:

- `claim_one`, `run_one`, `run_worker_once`, `sweep_stuck_tasks` — the
  worker primitives.
- `SourceRegistry` — `dict[str, BaseSource]`. The worker looks up the
  adapter for `task.source` here.
- `build_default_registry()` — production registry wired from settings.
"""

from __future__ import annotations

from discovery.sources.base import BaseSource
from discovery.workers.worker import (
    SourceRegistry,
    aclose_registry,
    claim_known_task,
    claim_one,
    run_one,
    run_worker_drain,
    run_worker_once,
    sweep_stuck_tasks,
)


def build_default_registry() -> SourceRegistry:
    """Production registry. Reads source credentials/UA strings from settings.

    Add new adapters here as they land. Each adapter is constructed once
    per worker process and reused for every task that targets it.
    """
    from discovery.config.settings import settings  # noqa: PLC0415 — lazy on purpose
    from discovery.sources.hackernews import HackerNewsSource  # noqa: PLC0415
    from discovery.sources.reddit import RedditSource  # noqa: PLC0415

    adapters: dict[str, BaseSource] = {
        "reddit": RedditSource(user_agent=settings.reddit_user_agent),
        "hackernews": HackerNewsSource(),  # no auth, no UA
    }
    return adapters


__all__ = [
    "SourceRegistry",
    "aclose_registry",
    "build_default_registry",
    "claim_known_task",
    "claim_one",
    "run_one",
    "run_worker_drain",
    "run_worker_once",
    "sweep_stuck_tasks",
]
