"""Command-line interface for Proofcast."""

from __future__ import annotations

import typer

from proofcast import DISCLAIMER, __version__

app = typer.Typer(
    name="proofcast",
    help=f"Proofcast: an AI market analyst that shows its own report card. {DISCLAIMER}",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"proofcast {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Proofcast command-line tool."""


@app.command()
def disclaimer() -> None:
    """Print the disclaimer that applies to everything Proofcast outputs."""
    typer.echo(DISCLAIMER)


def main() -> None:
    """Entry point for the `proofcast` console script."""
    app()
