"""`discovery jobs` / `discovery show` — read-only inspection CLI.

Two subcommands:

    discovery jobs              # one-line summary per Job
    discovery show <job_id>     # plan + top posts for one Job

Both render via Rich. The data layer is `discovery.view`.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from discovery.db.engine import async_session_factory, get_engine
from discovery.view import (
    JobDetail,
    JobSummary,
    PostView,
    gather_job_detail,
    gather_job_summaries,
)

console = Console()


# ---- discovery jobs ---------------------------------------------------------


def jobs_command() -> None:
    """List every Job in the database (one row per Job)."""
    asyncio.run(_run_jobs())


async def _run_jobs() -> None:
    engine = get_engine()
    try:
        async with async_session_factory(engine)() as session:
            rows = await gather_job_summaries(session)
    finally:
        await engine.dispose()

    if not rows:
        console.print(
            "[yellow]No jobs yet.[/yellow] "
            "Run [bold]uv run discovery run --industry \"...\"[/bold] to create one."
        )
        return

    table = Table(title="Discovery jobs", title_style="bold")
    table.add_column("id", justify="right", style="cyan")
    table.add_column("industry")
    table.add_column("as_of", style="dim")
    table.add_column("status")
    table.add_column("planned", justify="center")
    table.add_column("posts", justify="right", style="green")
    table.add_column("created", style="dim")

    for r in rows:
        table.add_row(
            str(r.id),
            r.industry,
            r.as_of,
            r.status,
            "[green]yes[/green]" if r.planned else "[yellow]no[/yellow]",
            str(r.post_count),
            r.created_at.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)
    console.print(
        "\n[dim]Inspect one: [bold]uv run discovery show <id>[/bold][/dim]"
    )


# ---- discovery show <id> ----------------------------------------------------


def show_command(
    job_id: int = typer.Argument(..., help="Job ID (see `discovery jobs`)."),
    post_limit: int = typer.Option(
        5,
        "--posts",
        "-n",
        help="How many top posts to print. Default 5.",
    ),
) -> None:
    """Show the LLM plan and top posts for one Job."""
    asyncio.run(_run_show(job_id, post_limit))


async def _run_show(job_id: int, post_limit: int) -> None:
    engine = get_engine()
    try:
        async with async_session_factory(engine)() as session:
            detail = await gather_job_detail(
                session, job_id=job_id, post_limit=post_limit
            )
    finally:
        await engine.dispose()

    if detail is None:
        console.print(
            f"[red]No job with id {job_id}.[/red] "
            "Run [bold]uv run discovery jobs[/bold] to see what's there."
        )
        raise typer.Exit(code=1)

    render_job_detail(detail)


# ---- shared renderer (used by `show` AND by `run` at the end of a run) ------


def render_job_detail(detail: JobDetail) -> None:
    """Print a Job's summary + plan + top posts to the console."""
    _render_summary(detail.summary)
    console.print()
    if detail.plan is not None:
        _render_plan(detail)
    else:
        console.print(
            "[yellow]No LLM plan stored.[/yellow] "
            "Wave 0 either fell back to the template, or this Job predates Wave 0."
        )
        console.print()
    _render_posts(detail.posts, total=detail.summary.post_count)


def _render_summary(s: JobSummary) -> None:
    planned = "[green]planned[/green]" if s.planned else "[yellow]fallback[/yellow]"
    console.print(
        Panel.fit(
            f"[bold]Job {s.id}[/bold]  ·  "
            f"[cyan]{s.industry}[/cyan]  ·  "
            f"as of [dim]{s.as_of}[/dim]\n"
            f"status: {s.status}  ·  wave 0: {planned}  ·  "
            f"posts: [green]{s.post_count}[/green]",
            title="summary",
            border_style="bright_blue",
        )
    )


def _render_plan(detail: JobDetail) -> None:
    assert detail.plan is not None
    plan = detail.plan
    console.print(
        f"[bold]LLM plan[/bold] ({len(plan.reddit_queries)} queries"
        + (
            f", {len(plan.reddit_subreddits)} subs shortlisted"
            if plan.reddit_subreddits
            else ""
        )
        + ")"
    )
    if plan.reddit_subreddits:
        subs = "  ".join(f"r/{s}" for s in plan.reddit_subreddits)
        console.print(f"  [dim]subreddits:[/dim] {subs}")
    console.print()

    table = Table(show_lines=False, header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("kind")
    table.add_column("target")
    table.add_column("rationale", overflow="fold")

    for i, q in enumerate(plan.reddit_queries, start=1):
        target = f"r/{q.subreddit}" if q.endpoint == "per_sub" else "(many subs)"
        table.add_row(str(i), q.endpoint, target, q.rationale)
    console.print(table)
    console.print()


def _render_posts(posts: list[PostView], *, total: int) -> None:
    if not posts:
        console.print("[yellow]No Reddit posts yet for this Job.[/yellow]")
        return
    console.print(f"[bold]Top {len(posts)} of {total} posts[/bold]")
    for p in posts:
        console.print()
        console.print(f"  [bold]{p.title}[/bold]")
        console.print(
            f"  [dim]r/{p.subreddit}  ·  {p.score} upvotes  ·  "
            f"{p.num_comments} comments[/dim]"
        )
        console.print(f"  [blue]https://reddit.com{p.permalink}[/blue]")
        if p.body_preview:
            console.print(f"  [italic dim]{p.body_preview}[/italic dim]")
