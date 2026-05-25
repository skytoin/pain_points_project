"""`discovery run` — end-to-end: create job, enqueue Reddit + HN + YouTube, dispatch all three.

Three phases:

1. **Setup** — create the job, run Wave 0 (LLM query expansion), enqueue
   all three source tasks in one session block. Capture the task ids.
2. **Parallel dispatch** — open a fresh session per concurrent branch and
   dispatch via `asyncio.gather`. `AsyncSession` is not safe to share across
   concurrent ops; each branch owns its session exclusively.
3. **Report** — open another fresh session for the read-only detail gather.

Usage::

    uv run discovery run --industry "commercial cleaning" \\
        --location NY --size medium --as-of 2026-06-01

`--as-of` defaults to today, so a monthly cron just invokes the command
without that flag — the date anchor ensures `spec_hash` differs across
months and each run produces a fresh `Job`.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import typer
from loguru import logger
from rich.console import Console

from discovery.cli.inspect import render_job_detail
from discovery.db.engine import async_session_factory, get_engine
from discovery.jobs import JobSpec, create_job
from discovery.orchestrator.hackernews import enqueue_hn_task_for_job
from discovery.orchestrator.jobs import plan_job
from discovery.orchestrator.reddit import enqueue_reddit_task_for_job
from discovery.orchestrator.youtube import enqueue_youtube_task_for_job
from discovery.view import gather_job_detail
from discovery.workers import (
    SourceRegistry,
    aclose_registry,
    build_default_registry,
    claim_known_task,
    run_one,
)

console = Console()


async def _run_discovery(
    industry: str,
    location: str | None,
    size: str | None,
    as_of: date,
    time_window: str,
) -> None:
    engine = get_engine()
    maker = async_session_factory(engine)
    registry = build_default_registry()

    spec = JobSpec(
        industry=industry,
        location=location,
        size=size,
        as_of=as_of,
        time_window=time_window,  # type: ignore[arg-type]
    )

    try:
        # Phase 1: create the job, run Wave 0 inline, enqueue ALL THREE
        # source tasks in one session block. Capture ids for Phase 2.
        async with maker() as session:
            job = await create_job(session, spec)
            console.print(
                f"[bold]job:[/bold] {job.id}  "
                f"[dim](spec_hash {job.spec_hash[:12]}…, "
                f"status {job.status.value})[/dim]"
            )

            # Wave 0: LLM query expansion via OpenAI gpt-5.4. On
            # success this populates job.job_plan with four fields
            # (reddit_queries, reddit_subreddits, hn_queries,
            # youtube_queries); on failure (no API key, LLM error,
            # validation drops too many queries) job.job_plan stays
            # null and ALL THREE orchestrators fall back to their
            # deterministic templates.
            job = await plan_job(session, job)
            plan_status = "planned" if job.job_plan is not None else "fallback"
            console.print(f"[bold]wave 0:[/bold] {plan_status}")

            reddit_task = await enqueue_reddit_task_for_job(session, job)
            hn_task = await enqueue_hn_task_for_job(session, job)
            youtube_task = await enqueue_youtube_task_for_job(session, job)
            console.print(
                f"[bold]queued tasks:[/bold] "
                f"reddit={reddit_task.id} "
                f"(queries={len(reddit_task.params['queries'])}), "
                f"hackernews={hn_task.id} "
                f"(queries={len(hn_task.params['queries'])}), "
                f"youtube={youtube_task.id} "
                f"(queries={len(youtube_task.params['queries'])})"
            )

            job_id = job.id
            reddit_task_id = reddit_task.id
            hn_task_id = hn_task.id
            youtube_task_id = youtube_task.id

        # Phase 2: parallel dispatch by known task id. Each branch
        # opens its own session -- AsyncSession is not safe to share
        # across concurrent ops, and `claim_known_task` is race-safe
        # per-id (it routes around the single-worker-safe `claim_one`).
        # `run_one` already catches and finalizes adapter failures
        # internally, so partial success across sources is automatic:
        # if one source fails entirely, the job still produces records
        # from the other two.
        console.print("[bold]running reddit + hackernews + youtube concurrently...[/bold]")
        assert reddit_task_id is not None
        assert hn_task_id is not None
        assert youtube_task_id is not None
        await asyncio.gather(
            _run_task_in_own_session(maker, registry, reddit_task_id),
            _run_task_in_own_session(maker, registry, hn_task_id),
            _run_task_in_own_session(maker, registry, youtube_task_id),
        )
        console.print("[bold green]done.[/bold green] 3 task(s) processed.")

        # Phase 3: report. Fresh session for the read-only detail
        # gather (the Phase-1 session was closed after enqueue).
        if job_id is not None:
            async with maker() as session:
                detail = await gather_job_detail(session, job_id=job_id, post_limit=5)
                if detail is not None:
                    console.print()
                    render_job_detail(detail)
    finally:
        await aclose_registry(registry)
        await engine.dispose()


async def _run_task_in_own_session(
    maker: Any,
    registry: SourceRegistry,
    task_id: int,
) -> None:
    """Open a fresh session, atomically claim the task by id, dispatch
    it. One session per concurrent branch (AsyncSession is not
    safe to share across concurrent ops). If the task is no longer
    queued by the time we get to it (very unlikely under
    single-worker, but defensive), log and return -- do not raise.
    """
    async with maker() as s:
        task = await claim_known_task(s, task_id)
        if task is None:
            logger.warning("task {} not claimed (already running/done?)", task_id)
            return
        await run_one(s, registry, task)


def run_command(
    industry: str = typer.Option(
        ...,
        "--industry",
        "-i",
        help='Free-form industry name, e.g. "commercial cleaning".',
    ),
    location: str | None = typer.Option(None, "--location", "-l", help="Optional location filter."),
    size: str | None = typer.Option(
        None,
        "--size",
        "-s",
        help='Optional size hint (e.g. "small", "medium").',
    ),
    as_of: str = typer.Option(
        "today",
        "--as-of",
        help="Date anchor in YYYY-MM-DD format. Defaults to today.",
    ),
    time_window: str = typer.Option(
        "month",
        "--time-window",
        "-t",
        help=(
            "Search time window: hour | day | week | month | year | all. "
            "Default month. Applies to ALL THREE sources via "
            "JobSpec.time_window: Reddit (`t` parameter), HackerNews "
            "(`numericFilters` recency floor), and YouTube (`publishedAfter` "
            "RFC 3339 floor). Use year for niche / B2B topics where a month "
            "doesn't produce enough signal."
        ),
    ),
) -> None:
    """Run a discovery slice: create the job, enqueue tasks, run Reddit + HN + YouTube concurrently."""
    anchor = date.today() if as_of == "today" else date.fromisoformat(as_of)
    asyncio.run(_run_discovery(industry, location, size, anchor, time_window))
