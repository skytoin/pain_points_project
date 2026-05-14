"""Orchestrator — turns a Job into a stream of queued Tasks.

Per-source modules generate that source's task(s) for a given Job. A
top-level `enqueue_all_for_job` helper will land here once we have more
than one source; for now Reddit is the only Wave 1 adapter.
"""

from __future__ import annotations

from discovery.orchestrator.reddit import (
    enqueue_reddit_task_for_job,
    reddit_queries_for_spec,
)

__all__ = [
    "enqueue_reddit_task_for_job",
    "reddit_queries_for_spec",
]
