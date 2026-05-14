"""`uv run python -m discovery.cli.init_db` — create the local DB.

This is intentionally a stub. Claude Code will fill in the real
implementation once `src/discovery/db/models.py` defines the tables.

For now it just prints a friendly message so the setup steps in
README.md don't fail.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from discovery.config.settings import settings

console = Console()


def main() -> None:
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    console.print(
        f"[yellow]init_db is a stub.[/yellow] "
        f"Wire it up after models.py defines tables.\n"
        f"  database_url: [dim]{settings.database_url}[/dim]"
    )


if __name__ == "__main__":
    main()
