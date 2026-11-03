"""Command-line interface for Proofcast."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import typer

from proofcast import DISCLAIMER, __version__
from proofcast.config import Config, load_config
from proofcast.llm.base import Provider, TokenBudget
from proofcast.models import Asset, Forecast
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


# ---------------------------------------------------------------------------
# analyze / forecast
# ---------------------------------------------------------------------------


def _as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        typer.echo("error: --as-of must be an ISO date or datetime", err=True)
        raise typer.Exit(2) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@app.command()
def analyze(
    asset: str = typer.Argument(..., help="Asset symbol."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Baseline only, no LLM."),
    model: str | None = typer.Option(None, "--model", help="Override PROOFCAST_MODEL."),
    as_of: str | None = typer.Option(
        None, "--as-of", help="Backtest time (never recorded; see the warning)."
    ),
    config: Path | None = ConfigOption,
) -> None:
    """Print a full analysis report for one asset (nothing is written to the ledger)."""
    from proofcast.agents.context import NoPriceDataError
    from proofcast.forecast.engine import make_forecasts, report_text

    cfg = _load(config)
    symbol = _assets(cfg, [asset])[0].symbol
    provider, budget = _provider(no_ai, model)
    try:
        result = make_forecasts(
            cfg, DataStore(cfg.data_dir), symbol, _as_of(as_of), provider, budget=budget
        )
    except NoPriceDataError as exc:
        typer.echo(f"error: {exc}. Run `proofcast fetch prices -a {symbol}` first.", err=True)
        raise typer.Exit(1) from exc
    typer.echo(report_text(result))


@app.command()
def forecast(
    all_assets: bool = typer.Option(False, "--all", help="Forecast every asset in the universe."),
    asset: list[str] | None = AssetOption,
    no_ai: bool = typer.Option(False, "--no-ai", help="Record baseline forecasts only."),
    model: str | None = typer.Option(None, "--model", help="Override PROOFCAST_MODEL."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show, but do not write."),
    config: Path | None = ConfigOption,
) -> None:
    """Make live forecasts and append them to the hash-chained ledger."""
    from proofcast.agents.context import NoPriceDataError
    from proofcast.forecast.engine import already_forecast, assert_live, make_forecasts
    from proofcast.ledger import Ledger, LedgerError

    cfg = _load(config)
    if not all_assets and not asset:
        typer.echo("error: pass --all or at least one --asset", err=True)
        raise typer.Exit(2)
    assets = _assets(cfg, None if all_assets else asset)
    provider, budget = _provider(no_ai, model)
    store = DataStore(cfg.data_dir)
    ledger = Ledger(cfg.ledger_path)
    try:
        existing = ledger.forecasts()
    except LedgerError as exc:
        typer.echo(f"error: ledger is broken: {exc}", err=True)
        raise typer.Exit(1) from exc
    now = datetime.now(UTC)
    new: list[Forecast] = []
    for a in assets:
        try:
            result = make_forecasts(cfg, store, a.symbol, now, provider, budget=budget)
        except NoPriceDataError as exc:
            typer.echo(f"skip {a.symbol}: {exc}")
            continue
        for f in result.forecasts:
            assert_live(f.created_at, datetime.now(UTC))
            if already_forecast(existing + new, f):
                typer.echo(f"skip {f.asset} {f.horizon_days}d: already forecast for this bar")
                continue
            new.append(f)
            typer.echo(
                f"{f.asset:>6} {f.horizon_days:>3}d  {f.probability:5.1%}  "
                f"(base rate {f.base_rate:.1%})  {f.setup}"
            )
    if dry_run:
        typer.echo(f"dry run: {len(new)} forecast(s) not written")
        return
    try:
        ledger.append(new)
    except LedgerError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"recorded {len(new)} forecast(s) in {cfg.ledger_path}")
    typer.echo(DISCLAIMER)


@app.command()
def doctor(
    model: str | None = typer.Option(None, "--model", help="Check this model spec instead."),
    live: bool = typer.Option(False, "--live", help="Also make one tiny LLM call."),
    config: Path | None = ConfigOption,
) -> None:
    """Check data sources and AI setup. Never prints keys."""
    from proofcast.doctor import run_doctor

    cfg = _load(config)
    rows, ok = run_doctor(cfg, config_path=config, model=model, live=live)
    for status, check, detail in rows:
        typer.echo(f"[{status:>4}] {check}: {detail}")
    if not ok:
        raise typer.Exit(1)


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
