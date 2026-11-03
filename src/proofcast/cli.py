"""Command-line interface for Proofcast."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from proofcast import DISCLAIMER, __version__
from proofcast.config import Config, load_config
from proofcast.llm.base import Provider, TokenBudget
from proofcast.models import Asset
from proofcast.storage import DataStore

app = typer.Typer(
    name="proofcast",
    help=f"Proofcast: an AI market analyst that shows its own report card. {DISCLAIMER}",
    no_args_is_help=True,
    add_completion=False,
)
fetch_app = typer.Typer(help="Fetch data from free sources into data/.", no_args_is_help=True)
app.add_typer(fetch_app, name="fetch")

ConfigOption = typer.Option(None, "--config", help="Path to proofcast.toml.")
AssetOption = typer.Option(None, "--asset", "-a", help="Asset symbol (repeatable).")
SinceOption = typer.Option(None, "--since", help="Start date, YYYY-MM-DD.")


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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load(config: Path | None) -> Config:
    try:
        return load_config(config)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: cannot load config: {exc}", err=True)
        raise typer.Exit(2) from exc


def _assets(cfg: Config, symbols: list[str] | None) -> list[Asset]:
    if not symbols:
        return list(cfg.assets)
    try:
        return [cfg.asset(s) for s in symbols]
    except KeyError as exc:
        typer.echo(f"error: {exc.args[0]}", err=True)
        raise typer.Exit(2) from exc


def _since(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        typer.echo("error: --since must be YYYY-MM-DD", err=True)
        raise typer.Exit(2) from exc


def _run_fetch(
    config: Path | None,
    what: list[str],
    symbols: list[str] | None,
    since: str | None,
    gdelt: bool = False,
) -> None:
    from proofcast.fetch import Fetcher, FetchReport, make_client

    cfg = _load(config)
    assets = _assets(cfg, symbols)
    start = _since(since)
    store = DataStore(cfg.data_dir)
    report = FetchReport()
    with make_client(cfg) as client:
        fetcher = Fetcher(cfg, store, client)
        if "prices" in what:
            report.merge(fetcher.prices(assets, start))
        if "news" in what:
            report.merge(fetcher.news(assets, since=start, gdelt=gdelt))
        if "filings" in what:
            report.merge(fetcher.filings(assets, start))
        if "macro" in what:
            report.merge(fetcher.macro(start))
    for line in report.lines():
        typer.echo(line)
    if report.errors and not any(report.added.values()):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


@fetch_app.command("prices")
def fetch_prices(
    asset: list[str] | None = AssetOption,
    since: str | None = SinceOption,
    config: Path | None = ConfigOption,
) -> None:
    """Fetch daily price bars."""
    _run_fetch(config, ["prices"], asset, since)


@fetch_app.command("news")
def fetch_news(
    asset: list[str] | None = AssetOption,
    since: str | None = SinceOption,
    gdelt: bool = typer.Option(False, "--gdelt", help="Also search GDELT (slow, rate limited)."),
    config: Path | None = ConfigOption,
) -> None:
    """Fetch news from the configured RSS feeds (and optionally GDELT)."""
    _run_fetch(config, ["news"], asset, since, gdelt=gdelt)


@fetch_app.command("filings")
def fetch_filings(
    asset: list[str] | None = AssetOption,
    since: str | None = SinceOption,
    config: Path | None = ConfigOption,
) -> None:
    """Fetch SEC filings and headline company facts (needs PROOFCAST_SEC_USER_AGENT)."""
    _run_fetch(config, ["filings"], asset, since)


@fetch_app.command("macro")
def fetch_macro(
    since: str | None = SinceOption,
    config: Path | None = ConfigOption,
) -> None:
    """Fetch macro series from FRED (FRED_API_KEY optional)."""
    _run_fetch(config, ["macro"], None, since)


@fetch_app.command("all")
def fetch_all(
    asset: list[str] | None = AssetOption,
    since: str | None = SinceOption,
    config: Path | None = ConfigOption,
) -> None:
    """Fetch prices, news, filings and macro data."""
    _run_fetch(config, ["prices", "news", "filings", "macro"], asset, since)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@app.command()
def history(
    asset: str = typer.Argument(..., help="Asset symbol, e.g. AAPL."),
    config: Path | None = ConfigOption,
) -> None:
    """List every major drawdown of the last 10 years (no AI needed)."""
    from proofcast.history.report import history_text

    cfg = _load(config)
    symbol = _assets(cfg, [asset])[0].symbol
    typer.echo(history_text(cfg, DataStore(cfg.data_dir), symbol))


@app.command("why")
def why_cmd(
    asset: str = typer.Argument(..., help="Asset symbol."),
    day: str = typer.Argument(..., help="A date inside the drawdown, YYYY-MM-DD."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Rules-based summary, no LLM."),
    model: str | None = typer.Option(None, "--model", help="Override PROOFCAST_MODEL."),
    config: Path | None = ConfigOption,
) -> None:
    """Explain a drawdown with the news, filings and macro releases around it."""
    from proofcast.history.report import why, why_text

    cfg = _load(config)
    symbol = _assets(cfg, [asset])[0].symbol
    when = _since(day)
    assert when is not None
    provider, budget = _provider(no_ai, model)
    result = why(cfg, DataStore(cfg.data_dir), symbol, when, provider, budget)
    if result is None:
        typer.echo(f"No major drawdown of {symbol} found around {when}. See `proofcast history`.")
        raise typer.Exit(1)
    typer.echo(why_text(*result))


def _provider(no_ai: bool, model: str | None) -> tuple[Provider | None, TokenBudget | None]:
    """Resolve the BYOK provider from flags and environment; None means AI is off."""
    from proofcast.llm.factory import ProviderConfigError, provider_from_env

    if no_ai:
        return None, None
    try:
        return provider_from_env(model=model)
    except ProviderConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc


def main() -> None:
    """Entry point for the `proofcast` console script."""
    app()
