"""`discovery run` — end-to-end: create job, enqueue Reddit, drain the queue.

This is the Wave 1 happy path with a single source. The orchestration
template is hand-rolled (see `discovery.orchestrator.reddit`); Wave 0
will replace it with the LLM query-expansion station later.

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

import typer
from rich.console import Console

from discovery.cli.inspect import render_job_detail
from discovery.db.engine import async_session_factory, get_engine
from discovery.jobs import JobSpec, create_job
from discovery.orchestrator.jobs import plan_job
from discovery.orchestrator.reddit import enqueue_reddit_task_for_job
from discovery.view import gather_job_detail
from discovery.workers import build_default_registry, run_worker_once

console = Console()


async def _run_discovery(
    industry: str,
    location: str | None,
    size: str | None,
    as_of: date,
) -> None:
    engine = get_engine()
    maker = async_session_factory(engine)
    registry = build_default_registry()

    spec = JobSpec(industry=industry, location=location, size=size, as_of=as_of)

    try:
        async with maker() as session:
            job = await create_job(session, spec)
            console.print(
                f"[bold]job:[/bold] {job.id}  "
                f"[dim](spec_hash {job.spec_hash[:12]}…, status {job.status.value})[/dim]"
            )

            # Wave 0: LLM query expansion via OpenAI gpt-5.4. On success
            # this populates job.job_plan; on failure (no API key, LLM
            # error, validation drops too many queries) the job_plan
            # stays null and the Reddit orchestrator falls back to its
            # deterministic template.
            job = await plan_job(session, job)
            plan_status = "planned" if job.job_plan is not None else "fallback"
            console.print(f"[bold]wave 0:[/bold] {plan_status}")

            task = await enqueue_reddit_task_for_job(session, job)
            console.print(
                f"[bold]queued task:[/bold] {task.id}  "
                f"[dim](source={task.source}, "
                f"queries={len(task.params['queries'])})[/dim]"
            )

            processed = 0
            while True:
                task_id = await run_worker_once(session, registry)
                if task_id is None:
                    break
                processed += 1
                console.print(f"  [green]✓[/green] processed task {task_id}")

            console.print(f"[bold green]done.[/bold green] {processed} task(s) processed.")

            # Print the full report — plan + top posts — so the user
            # doesn't need a second command to see what came back.
            if job.id is not None:
                detail = await gather_job_detail(session, job_id=job.id, post_limit=5)
                if detail is not None:
                    console.print()
                    render_job_detail(detail)
    finally:
        await engine.dispose()


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
) -> None:
    """Run a discovery slice: create the job, enqueue Reddit tasks, drain the queue."""
    anchor = date.today() if as_of == "today" else date.fromisoformat(as_of)
    asyncio.run(_run_discovery(industry, location, size, anchor))
