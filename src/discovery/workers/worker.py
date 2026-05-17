"""Worker bridge: claim tasks, dispatch to source adapters, persist results.

The worker is the seam between the task queue and the source adapters.
One process at a time per CLAUDE.md's single-worker assumption — the
claim path is safe under that constraint but would need
`SELECT ... FOR UPDATE SKIP LOCKED` (Postgres) or equivalent for
multi-worker safety.

Public surface
--------------
- `claim_one(session)` — atomically claim one queued task, or None.
- `run_one(session, registry, task)` — dispatch + persist + finalize.
- `run_worker_once(session, registry)` — `claim_one` + `run_one`.
- `run_worker_drain(session, registry)` — `run_worker_once` in a loop
  until the queue is empty; returns the count processed.
- `aclose_registry(registry)` — close every adapter (release owned HTTP
  clients) at worker shutdown.
- `sweep_stuck_tasks(session, idle_minutes)` — recover tasks orphaned by
  worker crashes; either requeue (retries left) or mark failed.
- `SourceRegistry` — alias for `dict[str, BaseSource]`. The worker
  resolves a task's adapter via `registry[task.source]`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from discovery.db.models import RawRecordRow, Task, TaskStatus
from discovery.hashing import hash_params
from discovery.sources.base import BaseSource, RawRecord

SourceRegistry = dict[str, BaseSource]


async def claim_one(session: AsyncSession) -> Task | None:
    """Atomically claim the next queued task (lowest wave, then oldest).

    Returns the claimed `Task` with status flipped to `running`,
    `claimed_at` stamped, and `attempts` incremented. Returns `None` if
    the queue is empty.

    Single-worker safe: the SELECT-then-UPDATE pair is wrapped in one
    transaction. For multi-worker concurrency, switch to a single
    `UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING *` statement
    (atomic in SQLite) or `FOR UPDATE SKIP LOCKED` (Postgres).
    """
    result = await session.exec(
        select(Task)
        .where(Task.status == TaskStatus.queued)
        .order_by(col(Task.wave), col(Task.created_at))
        .limit(1)
    )
    task = result.first()
    if task is None:
        return None

    task.status = TaskStatus.running
    task.claimed_at = datetime.now(UTC)
    task.attempts += 1
    await session.commit()
    await session.refresh(task)
    return task


async def run_one(
    session: AsyncSession,
    registry: SourceRegistry,
    task: Task,
) -> None:
    """Run a claimed task: dispatch to its source, persist results, finalize.

    Outcomes for the task:
    - Adapter returns records → write Bronze rows (deduped on
      `(source, external_id)`), mark task `done`.
    - Adapter raises and retries remain → set `last_error`, requeue.
    - Adapter raises with no retries left → set `last_error`, mark `failed`.
    - Source name is not in the registry → mark `failed` immediately
      (no point retrying a misconfiguration).
    """
    adapter = registry.get(task.source)
    if adapter is None:
        await _finalize_failure(session, task, f"unknown source: {task.source!r}", retriable=False)
        return

    try:
        records = await adapter.fetch(task.params)
        await _persist_records(session, task, records)
    except Exception as exc:
        await _finalize_failure(session, task, f"{type(exc).__name__}: {exc}", retriable=True)
        return

    task.status = TaskStatus.done
    task.completed_at = datetime.now(UTC)
    task.last_error = None
    await session.commit()


async def run_worker_once(
    session: AsyncSession,
    registry: SourceRegistry,
) -> int | None:
    """Claim and run one task. Returns its id, or `None` if the queue was empty."""
    task = await claim_one(session)
    if task is None:
        return None
    assert task.id is not None
    await run_one(session, registry, task)
    return task.id


async def sweep_stuck_tasks(
    session: AsyncSession,
    *,
    idle_minutes: int = 10,
) -> int:
    """Recover tasks orphaned by a worker crash.

    A task is "stuck" if it's been `running` longer than `idle_minutes`.
    For each one:

    - `attempts < max_attempts` → requeue (status back to `queued`,
      clear `claimed_at`) so a future worker picks it up.
    - `attempts >= max_attempts` → mark `failed` with an explanatory
      `last_error`; the orchestrator can decide what to do next.

    Returns the number of tasks touched.
    """
    threshold = datetime.now(UTC) - timedelta(minutes=idle_minutes)
    result = await session.exec(
        select(Task).where(
            Task.status == TaskStatus.running,
            col(Task.claimed_at) < threshold,
        )
    )
    touched = 0
    for task in result.all():
        if task.attempts < task.max_attempts:
            task.status = TaskStatus.queued
            task.claimed_at = None
            task.last_error = f"requeued by stuck-task sweep (idle > {idle_minutes} min)"
        else:
            task.status = TaskStatus.failed
            task.completed_at = datetime.now(UTC)
            task.last_error = (
                f"stuck running past max_attempts ({task.max_attempts}); marked failed by sweep"
            )
        touched += 1
    if touched:
        await session.commit()
    return touched


async def run_worker_drain(session: AsyncSession, registry: SourceRegistry) -> int:
    """Process queued tasks until the queue is empty; return the count.

    A one-shot drain (not a daemon): repeatedly calls `run_worker_once`
    until it returns `None`. Single-worker per CLAUDE.md — no polling,
    no concurrency.
    """
    processed = 0
    while await run_worker_once(session, registry) is not None:
        processed += 1
    return processed


async def aclose_registry(registry: SourceRegistry) -> None:
    """Close every adapter in the registry (release owned HTTP clients).

    Call once when a worker process shuts down. Adapters that own no
    resources inherit `BaseSource.aclose`'s no-op, so this is safe for
    every registry regardless of which adapters it holds.
    """
    for adapter in registry.values():
        await adapter.aclose()


# --- internals --------------------------------------------------------------


async def _persist_records(session: AsyncSession, task: Task, records: list[RawRecord]) -> None:
    """Insert Bronze rows; silently drop duplicates on (source, external_id).

    Uses SQLite's `INSERT ... ON CONFLICT DO NOTHING` for cheap dedup —
    when the worker re-runs the same task, or when two queries surface
    the same Reddit permalink, the duplicate just doesn't write.
    """
    if not records:
        return

    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = [
        {
            "job_id": task.job_id,
            "task_id": task.id,
            "source": record.source,
            "external_id": record.external_id,
            "fetched_at": record.fetched_at or now,
            "body": record.body,
            "content_hash": hash_params(record.body),
        }
        for record in records
    ]

    stmt = sqlite_insert(RawRecordRow).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["source", "external_id"])
    # `session.exec()` is SQLModel's typed wrapper; it works for INSERTs too.
    # Plain `session.execute()` triggers a SQLModel deprecation warning that
    # pyproject's `filterwarnings = ["error"]` would turn into a test failure.
    await session.exec(stmt)


async def _finalize_failure(
    session: AsyncSession,
    task: Task,
    error_message: str,
    *,
    retriable: bool,
) -> None:
    """Mark a task as failed or requeue it, depending on retry budget."""
    task.last_error = error_message
    if retriable and task.attempts < task.max_attempts:
        task.status = TaskStatus.queued
        task.claimed_at = None
        logger.warning(
            "task requeued",
            task_id=task.id,
            attempts=task.attempts,
            max_attempts=task.max_attempts,
            error=error_message,
        )
    else:
        task.status = TaskStatus.failed
        task.completed_at = datetime.now(UTC)
        logger.error(
            "task failed",
            task_id=task.id,
            attempts=task.attempts,
            error=error_message,
        )
    await session.commit()
