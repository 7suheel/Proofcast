"""Pydantic data models.

Every model that represents information from the outside world carries the time it became
public (``published_at`` or ``available_at``). The point-in-time layer (:mod:`proofcast.pit`)
uses these fields to guarantee that a forecast made at time T only sees data published strictly
before T (hard rule 4).

All datetimes are timezone-aware and normalised to UTC; naive datetimes are rejected.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

AssetKind = Literal["stock", "crypto"]


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
"""A timezone-aware datetime, normalised to UTC."""

Probability = Annotated[float, Field(ge=0.0, le=1.0)]

GENESIS_HASH = "0" * 64
"""``prev_hash`` of the first ledger entry."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def stable_id(*parts: str) -> str:
    """Return a short, deterministic id derived from ``parts`` (sha256, 16 hex chars)."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------


class Asset(_Model):
    """An asset in the universe, with the identifiers each data source needs."""

    symbol: str = Field(pattern=r"^[A-Z0-9.\-]{1,12}$")
    kind: AssetKind
    name: str
    aliases: list[str] = Field(default_factory=list)
    coingecko_id: str | None = None
    coinbase_product: str | None = None
    cik: str | None = Field(default=None, pattern=r"^\d{10}$")
    stooq_symbol: str | None = None
    yahoo_symbol: str | None = None


# ---------------------------------------------------------------------------
# Market and public information
# ---------------------------------------------------------------------------


def bar_available_at(day: date, kind: AssetKind) -> datetime:
    """Conservative time at which the daily bar for ``day`` is complete and public.

    * US stocks close at 16:00 New York time, which is 20:00 or 21:00 UTC depending on daylight
      saving. We use 22:00 UTC on the trading day, later than the close in every season.
    * Crypto daily candles cover a UTC calendar day, so they are complete at 00:00 UTC on the
      next day.
    """
    if kind == "stock":
        return datetime.combine(day, time(22, 0), tzinfo=UTC)
    return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=UTC)


class PriceBar(_Model):
    """One daily OHLCV bar."""

    asset: str
    date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    source: str
    fetched_at: UtcDatetime
    available_at: UtcDatetime
    """When the bar became public: the end of the trading session (see :func:`bar_available_at`)."""

    @model_validator(mode="after")
    def _check_range(self) -> PriceBar:
        lo, hi = self.low, self.high
        tolerance = 1e-9 * max(hi, 1.0)
        if hi + tolerance < lo:
            raise ValueError("high must be >= low")
        for name in ("open", "close"):
            value = getattr(self, name)
            if not (lo - tolerance <= value <= hi + tolerance):
                raise ValueError(f"{name} must lie between low and high")
        return self


class NewsItem(_Model):
    """A news headline from an RSS feed or GDELT. Untrusted text (hard rule 6)."""

    id: str
    title: str
    url: str
    source: str
    published_at: UtcDatetime
    fetched_at: UtcDatetime
    assets: list[str] = Field(default_factory=list)
    summary: str | None = None
    """Raw summary text. Pruned after 90 days to keep the repository small."""

    @classmethod
    def make_id(cls, url: str, title: str) -> str:
        """Deterministic id for a news item."""
        return stable_id("news", url.strip(), title.strip())


class Filing(_Model):
    """An SEC filing (8-K, 10-Q, 10-K, ...)."""

    id: str
    """SEC accession number, for example ``0000320193-24-000123``."""
    company: str
    """Asset symbol of the filer."""
    cik: str
    form_type: str
    filed_at: UtcDatetime
    """SEC acceptance time: when the filing became public on EDGAR."""
    url: str
    items: list[str] = Field(default_factory=list)
    """8-K item numbers such as ``2.02`` (results of operations)."""
    title: str | None = None
    fetched_at: UtcDatetime

    @property
    def published_at(self) -> datetime:
        """Alias of :attr:`filed_at`, so filings fit the point-in-time interface."""
        return self.filed_at

    @property
    def display_title(self) -> str | None:
        """The SEC description, unless it only repeats the form type (e.g. "10-K")."""
        if self.title and self.title.strip().upper() != self.form_type.strip().upper():
            return self.title.strip()
        return None


class FinancialFact(_Model):
    """One reported XBRL value from SEC company facts (revenue, net income, EPS, ...)."""

    asset: str
    concept: str
    unit: str
    value: float
    period_end: date
    fiscal_period: str | None
    form: str
    filed: date
    accession: str
    published_at: UtcDatetime
    """End of the filing day (US Eastern, conservative): facts are public by then."""


class MacroObservation(_Model):
    """One observation of a macro series (FRED)."""

    series_id: str
    date: date
    """Start of the observation period (FRED convention)."""
    value: float
    published_at: UtcDatetime
    published_at_estimated: bool
    """True when ``published_at`` is a conservative estimate (no FRED vintage data)."""
    source: str
    fetched_at: UtcDatetime


class SourceRef(_Model):
    """A citation: a piece of evidence an agent or report relied on."""

    id: str
    kind: Literal["news", "filing", "macro", "price", "indicator"]
    title: str
    url: str | None = None
    published_at: UtcDatetime


class Event(_Model):
    """A piece of evidence linked to a drawdown, ranked by relevance."""

    id: str
    kind: Literal["news", "filing", "macro"]
    asset: str
    title: str
    url: str | None = None
    source: str
    published_at: UtcDatetime
    relevance: float = Field(ge=0.0)
    days_from_start: float
    """Days between the drawdown start and the event (negative = before the start)."""


class Drawdown(_Model):
    """A peak-to-trough fall in daily closes deeper than a threshold."""

    asset: str
    start: date
    """Date of the peak close before the fall."""
    trough: date
    recovery: date | None
    """First date the close got back to the peak, or None if it has not recovered."""
    peak_close: float = Field(gt=0)
    trough_close: float = Field(gt=0)
    depth_pct: float = Field(gt=0, le=100)
    """Depth of the fall in percent of the peak close (positive number)."""

    @property
    def id(self) -> str:
        return f"{self.asset}-{self.start.isoformat()}"


# ---------------------------------------------------------------------------
# Agents and forecasts
# ---------------------------------------------------------------------------


class AgentOpinion(_Model):
    """What one analyst agent concluded, after our code validated its citations."""

    agent: str
    probabilities: dict[int, Probability] = Field(default_factory=dict)
    """Probability that the asset closes higher, keyed by horizon in days."""
    confidence: Probability = 0.5
    summary: str = ""
    citations: list[str] = Field(default_factory=list)
    """Source ids from the context that the agent cited and our code accepted."""
    dropped_citations: list[str] = Field(default_factory=list)
    """Cited ids that were not in the context; dropped, and confidence was lowered."""
    weight: float = Field(default=1.0, ge=0.0)
    """Weight from the agent's past track record (see :mod:`proofcast.weighting`)."""


class ResolutionRule(_Model):
    """Machine-checkable rule that resolves a forecast from price data only.

    The forecast resolves YES when the close of the first daily bar dated on or after
    ``target_date`` is strictly greater than ``reference_close`` (the close of the bar dated
    ``reference_date``, the last complete bar before the forecast was made).
    """

    type: Literal["close_above_reference"] = "close_above_reference"
    asset: str
    reference_date: date
    reference_close: float = Field(gt=0)
    target_date: date
    target_rule: Literal["first_bar_on_or_after"] = "first_bar_on_or_after"
    comparison: Literal[">"] = ">"
    max_wait_days: int = Field(default=10, ge=0)
    """If no bar exists within this many days after ``target_date``, the forecast is void."""


class Forecast(_Model):
    """A probabilistic forecast committed to the ledger before its outcome is known."""

    kind: Literal["forecast"] = "forecast"
    id: str
    created_at: UtcDatetime
    asset: str
    question: str
    horizon_days: int = Field(gt=0)
    probability: Probability
    base_rate: Probability
    baselines: dict[str, Probability] = Field(default_factory=dict)
    """Baseline probabilities for the same question at the same time (``always_50``,
    ``base_rate``, ``momentum``), so the track record compares like with like."""
    resolution_rule: ResolutionRule
    setup: str
    """Which forecasting setup produced this, e.g. ``ai:anthropic:<model>`` or
    ``baseline:momentum``. Scores are grouped by setup."""
    is_baseline: bool
    agent_opinions: list[AgentOpinion] = Field(default_factory=list)
    final_reasoning: str = ""
    sources: list[SourceRef] = Field(default_factory=list)
    model: str | None = None
    untrusted_input_notes: list[str] = Field(default_factory=list)
    """Notes about untrusted input handling, e.g. dropped citations or flagged injection text."""
    prev_hash: str = GENESIS_HASH
    hash: str = ""


class ForecastResolution(_Model):
    """The outcome of a forecast, computed from price data only (no AI)."""

    kind: Literal["resolution"] = "resolution"
    id: str
    forecast_id: str
    resolved_at: UtcDatetime
    status: Literal["resolved", "void"]
    outcome: bool | None
    """True if the asset closed higher, None if void."""
    target_bar_date: date | None
    target_close: float | None
    reference_close: float
    price_source: str | None
    note: str = ""
    prev_hash: str = GENESIS_HASH
    hash: str = ""


class Correction(_Model):
    """A note that references an earlier ledger entry.

    Corrections never edit, delete or remove an entry from scoring; they only add context.
    """

    kind: Literal["correction"] = "correction"
    id: str
    created_at: UtcDatetime
    refers_to: str
    reason: str
    prev_hash: str = GENESIS_HASH
    hash: str = ""


LedgerEntry = Annotated[Forecast | ForecastResolution | Correction, Field(discriminator="kind")]


class CalibrationBucket(_Model):
    """Forecasts whose probability falls in ``[lower, upper)`` (the last bucket includes 1.0)."""

    lower: float
    upper: float
    n: int
    mean_probability: float | None
    observed_frequency: float | None


class ScoreSummary(_Model):
    """Scores of one group of resolved forecasts (by setup, asset and/or horizon)."""

    group: str
    setup: str
    asset: str | None = None
    horizon_days: int | None = None
    n_forecasts: int
    n_resolved: int
    brier: float | None
    log_score: float | None
    """Mean log score (natural log of the probability given to the outcome; higher is better)."""
    hit_rate: float | None
    """Share of resolved forecasts on the right side of 50%."""
    calibration: list[CalibrationBucket] = Field(default_factory=list)
