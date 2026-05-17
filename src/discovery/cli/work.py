"""`discovery work` — drain the task queue.

One-shot: claims and runs every queued task (claim → dispatch to the
source adapter → persist to Bronze) until the queue is empty, then
exits. `run_worker_once` already exists; this is just the loop around
it (`run_worker_drain`) plus process setup/teardown. Single-worker per
CLAUDE.md — no polling, no concurrency.

Use it after `discovery run` enqueued tasks, or to clear a backlog:

    uv run discovery work
"""

from __future__ import annotations

import asyncio

from rich.console import Console

from discovery.db.engine import async_session_factory, get_engine
from discovery.workers import aclose_registry, build_default_registry, run_worker_drain

console = Console()


async def _run_work() -> None:
    engine = get_engine()
    maker = async_session_factory(engine)
    registry = build_default_registry()
    try:
        async with maker() as session:
            processed = await run_worker_drain(session, registry)
        console.print(f"[bold green]done.[/bold green] {processed} task(s) processed.")
    finally:
        await aclose_registry(registry)
        await engine.dispose()


def work_command() -> None:
    """Drain the task queue: process all queued tasks, then exit."""
    asyncio.run(_run_work())
