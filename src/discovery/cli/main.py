"""Command-line interface for the pipeline.

Invoke with:

    uv run discovery <subcommand>
    uv run python -m discovery.cli.main <subcommand>

Subcommands are organized into separate modules under
`src/discovery/cli/`. This file just wires them together.
"""

from __future__ import annotations

import typer
from rich.console import Console

from discovery import __version__
from discovery.cli.run import run_command

app = typer.Typer(
    name="discovery",
    help="Industry discovery pipeline.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the package version and exit."""
    console.print(f"discovery [bold cyan]{__version__}[/bold cyan]")


@app.command()
def hello(name: str = "world") -> None:
    """Smoke-test command — prints a greeting."""
    console.print(f"hello, [bold]{name}[/bold]")


app.command(name="run")(run_command)


if __name__ == "__main__":
    app()
