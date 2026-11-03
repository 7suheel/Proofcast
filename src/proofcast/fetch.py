"""Fetch orchestration: run adapters and store results.

Every step degrades gracefully: a failing or unconfigured source is reported and skipped, and
the other sources still run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from proofcast.adapters import fred, news, prices, sec
from proofcast.adapters.base import Clock, env_value, utcnow
from proofcast.adapters.mapping import AssetMatcher
from proofcast.config import Config
from proofcast.models import Asset, NewsItem
from proofcast.net.http import HttpClient, HttpError
from proofcast.storage import DataStore

PRICE_OVERLAP_DAYS = 5


@dataclass
class FetchReport:
    """What a fetch did: counts per target and any errors (already redacted)."""

    added: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def merge(self, other: FetchReport) -> FetchReport:
        for k, v in other.added.items():
            self.added[k] = self.added.get(k, 0) + v
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        return self

    def lines(self) -> list[str]:
        out = [f"{k}: {v} new or changed" for k, v in sorted(self.added.items())]
        out += [f"warning: {w}" for w in self.warnings]
        out += [f"error: {e}" for e in self.errors]
        return out


def make_client(cfg: Config, *, use_cache: bool = True) -> HttpClient:
    return HttpClient(cache_dir=cfg.cache_dir if use_cache else None)


class Fetcher:
    """Runs the adapters configured in ``proofcast.toml``."""

    def __init__(
        self,
        cfg: Config,
        store: DataStore,
        client: HttpClient,
        *,
        environ: dict[str, str] | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.environ = environ
        self.clock = clock

    def _history_start(self) -> date:
        return self.clock().date() - timedelta(days=round(365.25 * self.cfg.history.years) + 30)

    # -- prices --------------------------------------------------------------------

    def prices(self, assets: list[Asset], since: date | None = None) -> FetchReport:
        report = FetchReport()
        for asset in assets:
            start = since
            if start is None:
                existing = self.store.read_bars(asset.symbol)
                start = (
                    existing[-1].date - timedelta(days=PRICE_OVERLAP_DAYS)
                    if existing
                    else self._history_start()
                )
            if asset.kind == "crypto":
                providers: list[str] = list(self.cfg.sources.crypto.providers)
            else:
                providers = list(self.cfg.sources.stocks.providers)
            bars, errors = prices.fetch_prices(
                self.client,
                asset,
                start,
                providers,
                allow_unofficial=self.cfg.sources.stocks.allow_unofficial,
                clock=self.clock,
                environ=self.environ,
            )
            if bars:
                report.added[f"prices/{asset.symbol}"] = self.store.write_bars(asset.symbol, bars)
                report.warnings.extend(f"{asset.symbol}: {e}" for e in errors)
            else:
                report.errors.append(f"{asset.symbol}: no prices ({'; '.join(errors)})")
        return report

    # -- news ----------------------------------------------------------------------

    def news(
        self,
        assets: list[Asset],
        *,
        since: date | None = None,
        gdelt: bool = False,
    ) -> FetchReport:
        report = FetchReport()
        matcher = AssetMatcher(self.cfg.assets)
        wanted = {a.symbol for a in assets}
        items: list[NewsItem] = []
        for feed in self.cfg.sources.rss:
            try:
                got = news.fetch_feed(self.client, feed, matcher, clock=self.clock)
            except HttpError as exc:
                report.errors.append(f"rss {feed.name}: {exc}")
                continue
            items.extend(i for i in got if wanted & set(i.assets))
        if gdelt and self.cfg.sources.gdelt.enabled:
            now = self.clock()
            start = datetime.combine(since, time(0), UTC) if since else now - timedelta(days=7)
            for asset in assets:
                try:
                    items.extend(
                        news.fetch_gdelt(
                            self.client,
                            asset,
                            matcher,
                            start,
                            now,
                            min_interval=self.cfg.sources.gdelt.min_interval_seconds,
                            clock=self.clock,
                        )
                    )
                except HttpError as exc:
                    report.errors.append(f"gdelt {asset.symbol}: {exc}")
        report.added["news"] = self.store.write_news(items)
        pruned = self.store.prune_news_text(self.clock())
        if pruned:
            report.added["news text pruned"] = pruned
        return report

    # -- filings ---------------------------------------------------------------------

    def filings(self, assets: list[Asset], since: date | None = None) -> FetchReport:
        report = FetchReport()
        stocks = [a for a in assets if a.kind == "stock" and a.cik]
        if not stocks:
            return report
        try:
            sec.sec_user_agent(self.environ)
        except sec.SecUserAgentError as exc:
            report.warnings.append(f"filings skipped: {exc}")
            return report
        start = since or self._history_start()
        for asset in stocks:
            try:
                filings = sec.fetch_filings(
                    self.client, asset, since=start, environ=self.environ, clock=self.clock
                )
                report.added[f"filings/{asset.symbol}"] = self.store.write_filings(filings)
                facts = sec.fetch_company_facts(
                    self.client, asset, since=start, environ=self.environ
                )
                report.added[f"fundamentals/{asset.symbol}"] = self.store.write_facts(
                    asset.symbol, facts
                )
            except (HttpError, ValueError) as exc:
                report.errors.append(f"sec {asset.symbol}: {exc}")
        return report

    # -- macro -----------------------------------------------------------------------

    def macro(self, since: date | None = None) -> FetchReport:
        report = FetchReport()
        if not env_value(self.environ, fred.API_KEY_ENV):
            report.warnings.append(
                "FRED_API_KEY not set: using the public fredgraph.csv download with estimated "
                "publication times"
            )
        start = since or self._history_start()
        for series in self.cfg.macro:
            try:
                obs = fred.fetch_macro(
                    self.client, series, since=start, environ=self.environ, clock=self.clock
                )
            except (HttpError, ValueError) as exc:
                report.errors.append(f"fred {series.series_id}: {exc}")
                continue
            report.added[f"macro/{series.series_id}"] = self.store.write_macro(
                series.series_id, obs
            )
        return report
