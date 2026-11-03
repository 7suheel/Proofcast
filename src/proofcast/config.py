"""Loading ``proofcast.toml``.

The config file is looked up in this order: an explicit path, the ``PROOFCAST_CONFIG``
environment variable, ``./proofcast.toml``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from proofcast.models import Asset

CONFIG_ENV = "PROOFCAST_CONFIG"
DEFAULT_CONFIG_NAME = "proofcast.toml"


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(_Cfg):
    """Project-wide paths and URLs."""

    name: str = "Proofcast"
    site_url: str = ""
    repo_url: str = ""
    data_dir: str = "data"
    ledger_path: str = "ledger/forecasts.jsonl"
    cache_dir: str = ".cache/http"


class ForecastConfig(_Cfg):
    """Forecast horizons and probability bounds."""

    horizons: list[int] = Field(default_factory=lambda: [7, 30])
    min_probability: float = 0.05
    max_probability: float = 0.95

    @model_validator(mode="after")
    def _check(self) -> ForecastConfig:
        if not 0.0 < self.min_probability < 0.5 < self.max_probability < 1.0:
            raise ValueError("need 0 < min_probability < 0.5 < max_probability < 1")
        if not self.horizons or any(h <= 0 for h in self.horizons):
            raise ValueError("horizons must be positive")
        return self


class HistoryConfig(_Cfg):
    """Drawdown detection and evidence linking."""

    years: int = 10
    stock_drawdown_threshold: float = 0.20
    crypto_drawdown_threshold: float = 0.35
    evidence_days_before: int = 7
    stock_benchmark: str = "SP500"
    crypto_benchmark: str = "BTC"


class SchedulesConfig(_Cfg):
    """Cron expressions, for documentation; workflows are the source of truth on GitHub."""

    news: str = ""
    daily_data: str = ""
    crypto_data: str = ""
    forecasts: str = ""
    resolve_score: str = ""
    site: str = ""


StockProvider = Literal["stooq", "yahoo"]
CryptoProvider = Literal["coinbase", "coingecko"]


def _default_stock_providers() -> list[StockProvider]:
    return ["stooq"]


def _default_crypto_providers() -> list[CryptoProvider]:
    return ["coinbase", "coingecko"]


class StockSourcesConfig(_Cfg):
    providers: list[StockProvider] = Field(default_factory=_default_stock_providers)
    allow_unofficial: bool = False


class CryptoSourcesConfig(_Cfg):
    providers: list[CryptoProvider] = Field(default_factory=_default_crypto_providers)


class GdeltConfig(_Cfg):
    enabled: bool = True
    min_interval_seconds: float = 5.5


class RssFeed(_Cfg):
    name: str
    url: str


class SourcesConfig(_Cfg):
    stocks: StockSourcesConfig = Field(default_factory=StockSourcesConfig)
    crypto: CryptoSourcesConfig = Field(default_factory=CryptoSourcesConfig)
    gdelt: GdeltConfig = Field(default_factory=GdeltConfig)
    rss: list[RssFeed] = Field(default_factory=list)


class MacroSeries(_Cfg):
    """A FRED series and how long after its period it is normally published."""

    series_id: str
    name: str
    frequency: Literal["daily", "monthly"]
    release_lag_days: int = Field(ge=0)


class Config(_Cfg):
    """The whole configuration file."""

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    schedules: SchedulesConfig = Field(default_factory=SchedulesConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    assets: list[Asset] = Field(default_factory=list)
    macro: list[MacroSeries] = Field(default_factory=list)
    root: Path = Field(default=Path(), exclude=True)
    """Directory of the config file; relative paths are resolved against it."""

    @model_validator(mode="after")
    def _unique_assets(self) -> Config:
        symbols = [a.symbol for a in self.assets]
        if len(symbols) != len(set(symbols)):
            raise ValueError("asset symbols must be unique")
        return self

    def asset(self, symbol: str) -> Asset:
        """Return the asset with ``symbol`` (case-insensitive) or raise KeyError."""
        wanted = symbol.upper()
        for a in self.assets:
            if a.symbol == wanted:
                return a
        raise KeyError(f"unknown asset {symbol!r}; known: {', '.join(self.symbols())}")

    def symbols(self) -> list[str]:
        return [a.symbol for a in self.assets]

    def path(self, relative: str) -> Path:
        """Resolve a configured path relative to the config file's directory."""
        p = Path(relative)
        return p if p.is_absolute() else self.root / p

    @property
    def data_dir(self) -> Path:
        return self.path(self.project.data_dir)

    @property
    def ledger_path(self) -> Path:
        return self.path(self.project.ledger_path)

    @property
    def cache_dir(self) -> Path:
        return self.path(self.project.cache_dir)


def find_config(path: Path | None = None) -> Path | None:
    """Return the config path to use, or None if there is none."""
    if path is not None:
        return path
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env)
    candidate = Path.cwd() / DEFAULT_CONFIG_NAME
    return candidate if candidate.exists() else None


def load_config(path: Path | None = None) -> Config:
    """Load and validate the configuration. Without a file, return defaults rooted at cwd."""
    found = find_config(path)
    if found is None:
        return Config(root=Path.cwd())
    with found.open("rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config.model_validate(raw)
    return cfg.model_copy(update={"root": found.resolve().parent})
