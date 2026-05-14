"""`uv run python -m discovery.cli.init_db` — create / migrate the local DB.

Runs `alembic upgrade head` so users don't need to know about alembic
directly. Idempotent: re-running on an already-migrated DB is a no-op.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from rich.console import Console

from discovery.config.settings import settings

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg


def main() -> None:
    """Create the data dir if needed, then run alembic upgrade head."""
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    console.print(
        f"[dim]database_url:[/dim] {settings.database_url}\n"
        "[bold]Running alembic upgrade head…[/bold]"
    )
    command.upgrade(_alembic_config(), "head")
    console.print("[green]done.[/green]")


if __name__ == "__main__":
    main()
