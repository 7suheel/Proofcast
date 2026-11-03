"""Link a drawdown to the news, filings and macro releases around it.

Evidence window: from ``evidence_days_before`` days before the drawdown start until the end
of the trough day.

Relevance (documented in docs/methodology.md)::

    relevance = kind_weight * closeness * (1 + 0.5 * negative_words) * (1 + reaction)

* ``kind_weight``: 8-K with item 2.02 (results) 1.0; other 8-K 0.8; 10-Q/10-K 0.7; news 0.6;
  CPI/unemployment/fed funds release 0.4. Macro releases only count when they are a
  notable move (CPI month-on-month change of at least 0.4%, unemployment or fed funds
  change of at least 0.2 points); routine releases are not evidence.
* ``closeness``: 1.0 inside [start, trough], ``exp(-days_before_start / 3)`` before the start.
* ``negative_words``: 1 if the headline contains words such as "plunge", "miss", "probe".
* ``reaction``: the asset's next-day fall after the event, in units of its typical daily move
  (capped at 2), so items followed by a sharp drop rank higher.

Market context compares the asset's fall with a benchmark over the same dates (S&P 500 for
stocks, BTC for other crypto; BTC itself is compared with the S&P 500).
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Literal

from proofcast.config import Config
from proofcast.models import Drawdown, Event, MacroObservation, PriceBar
from proofcast.storage import DataStore

NEGATIVE_WORDS = re.compile(
    r"\b(?:plunge[sd]?|plummet\w*|slump\w*|tumble[sd]?|sink[s]?|sank|fall[s]?|fell|drop\w*|"
    r"crash\w*|sell-?off|rout|miss(?:es|ed)?|cut[s]?|warn\w*|downgrade[sd]?|probe|lawsuit|"
    r"sue[sd]?|recall\w*|investigation|fraud|hack\w*|ban(?:s|ned)?|bankrupt\w*|default\w*|"
    r"layoff[s]?|halt\w*|delay\w*|weak\w*|fear[s]?|loss(?:es)?|decline[sd]?)\b",
    re.IGNORECASE,
)
# series id -> (weight, minimum notable change, change measured in percent?)
MACRO_EVENT_SERIES: dict[str, tuple[float, float, bool]] = {
    "CPIAUCSL": (0.45, 0.4, True),
    "UNRATE": (0.4, 0.2, False),
    "FEDFUNDS": (0.4, 0.2, False),
}
MAX_EVENTS = 25

ContextLabel = Literal["market-wide", "partly market-wide", "mostly asset-specific", "unknown"]


@dataclass(frozen=True)
class MarketContext:
    """How the benchmark moved over the drawdown window."""

    benchmark: str
    asset_change_pct: float
    benchmark_change_pct: float | None
    label: ContextLabel

    def sentence(self) -> str:
        if self.benchmark_change_pct is None:
            return f"No {self.benchmark} data for this window, so market context is unknown."
        return (
            f"Over the same dates the asset moved {self.asset_change_pct:+.1f}% and "
            f"{self.benchmark} moved {self.benchmark_change_pct:+.1f}%: {self.label}."
        )


@dataclass(frozen=True)
class DrawdownEvidence:
    """A drawdown with its ranked evidence and market context."""

    drawdown: Drawdown
    window_start: datetime
    window_end: datetime
    events: list[Event] = field(default_factory=list)
    context: MarketContext | None = None

    @property
    def is_thin(self) -> bool:
        return len(self.events) < 3


def classify_context(asset_change: float, bench_change: float | None) -> ContextLabel:
    """Label a fall by how much of it the benchmark shared (both in percent, negative = fall)."""
    if bench_change is None or asset_change >= 0:
        return "unknown"
    share = max(0.0, -bench_change) / -asset_change
    if share >= 0.5:
        return "market-wide"
    if share >= 0.25:
        return "partly market-wide"
    return "mostly asset-specific"


def _change(series: Sequence[tuple[date, float]], start: date, end: date) -> float | None:
    """Percent change from the last value on/before ``start`` to the last on/before ``end``."""
    before = [v for d, v in series if d <= start]
    upto = [v for d, v in series if d <= end]
    if not before or not upto:
        return None
    return (upto[-1] / before[-1] - 1) * 100


def _closeness(t: datetime, start: datetime, trough_end: datetime) -> float:
    if start <= t <= trough_end:
        return 1.0
    days_before = (start - t).total_seconds() / 86400
    return math.exp(-max(days_before, 0.0) / 3)


def _typical_move(bars: Sequence[PriceBar]) -> float:
    moves = [abs(b.close / a.close - 1) for a, b in itertools.pairwise(bars)]
    return median(moves) if moves else 0.02


def _reaction(bars: Sequence[PriceBar], t: datetime, typical: float) -> float:
    """Next-day fall after time ``t`` in units of the typical daily move (0..2)."""
    before = [b for b in bars if b.available_at <= t]
    after = [b for b in bars if b.available_at > t]
    if not before or not after or typical <= 0:
        return 0.0
    change = after[0].close / before[-1].close - 1
    return min(2.0, max(0.0, -change) / typical)


def link_evidence(
    drawdown: Drawdown,
    store: DataStore,
    cfg: Config,
    *,
    bars: Sequence[PriceBar] | None = None,
) -> DrawdownEvidence:
    """Collect and rank evidence for ``drawdown`` from stored data."""
    bars = list(bars if bars is not None else store.read_bars(drawdown.asset))
    start = datetime.combine(drawdown.start, time(0), UTC)
    window_start = start - timedelta(days=cfg.history.evidence_days_before)
    window_end = datetime.combine(drawdown.trough + timedelta(days=1), time(0), UTC)
    typical = _typical_move(bars)

    def in_window(t: datetime) -> bool:
        return window_start <= t < window_end

    def days_from_start(t: datetime) -> float:
        return round((t - start).total_seconds() / 86400, 2)

    events: list[Event] = []
    for n in store.read_news():
        if drawdown.asset not in n.assets or not in_window(n.published_at):
            continue
        negative = 1.0 if NEGATIVE_WORDS.search(n.title) else 0.0
        rel = (
            0.6
            * _closeness(n.published_at, start, window_end)
            * (1 + 0.5 * negative)
            * (1 + _reaction(bars, n.published_at, typical))
        )
        events.append(
            Event(
                id=f"news:{n.id}",
                kind="news",
                asset=drawdown.asset,
                title=n.title,
                url=n.url,
                source=n.source,
                published_at=n.published_at,
                relevance=round(rel, 4),
                days_from_start=days_from_start(n.published_at),
            )
        )
    for f in store.read_filings():
        if f.company != drawdown.asset or not in_window(f.filed_at):
            continue
        if f.form_type.startswith("8-K"):
            weight = 1.0 if "2.02" in f.items else 0.8
        elif f.form_type.startswith(("10-Q", "10-K", "20-F", "6-K")):
            weight = 0.7
        else:
            continue
        rel = (
            weight
            * _closeness(f.filed_at, start, window_end)
            * (1 + _reaction(bars, f.filed_at, typical))
        )
        items = f" items {', '.join(f.items)}" if f.items else ""
        events.append(
            Event(
                id=f"filing:{f.id}",
                kind="filing",
                asset=drawdown.asset,
                title=f"{f.form_type}{items}" + (f": {f.title}" if f.title else ""),
                url=f.url,
                source="SEC EDGAR",
                published_at=f.filed_at,
                relevance=round(rel, 4),
                days_from_start=days_from_start(f.filed_at),
            )
        )
    for series_id, (weight, min_change, as_pct) in MACRO_EVENT_SERIES.items():
        obs = store.read_macro(series_id)
        for i, o in enumerate(obs):
            if not in_window(o.published_at) or i == 0:
                continue
            prev = obs[i - 1]
            change = (
                (o.value / prev.value - 1) * 100
                if as_pct and prev.value
                else (o.value - prev.value)
            )
            if abs(change) < min_change:
                continue
            events.append(_macro_event(drawdown, o, prev, weight, start, window_end, bars, typical))
    events.sort(key=lambda e: (-e.relevance, e.published_at))
    return DrawdownEvidence(
        drawdown=drawdown,
        window_start=window_start,
        window_end=window_end,
        events=events[:MAX_EVENTS],
        context=market_context(drawdown, store, cfg, bars),
    )


def _macro_event(
    drawdown: Drawdown,
    o: MacroObservation,
    prev: MacroObservation | None,
    weight: float,
    start: datetime,
    window_end: datetime,
    bars: Sequence[PriceBar],
    typical: float,
) -> Event:
    delta = f" (previous {prev.value:g})" if prev else ""
    estimated = " [release time estimated]" if o.published_at_estimated else ""
    rel = (
        weight
        * _closeness(o.published_at, start, window_end)
        * (1 + _reaction(bars, o.published_at, typical))
    )
    return Event(
        id=f"macro:{o.series_id}:{o.date.isoformat()}",
        kind="macro",
        asset=drawdown.asset,
        title=f"{o.series_id} for {o.date:%Y-%m}: {o.value:g}{delta}{estimated}",
        url=f"https://fred.stlouisfed.org/series/{o.series_id}",
        source="FRED",
        published_at=o.published_at,
        relevance=round(rel, 4),
        days_from_start=round((o.published_at - start).total_seconds() / 86400, 2),
    )


def market_context(
    drawdown: Drawdown, store: DataStore, cfg: Config, bars: Sequence[PriceBar]
) -> MarketContext:
    asset = cfg.asset(drawdown.asset) if drawdown.asset in cfg.symbols() else None
    asset_change = (drawdown.trough_close / drawdown.peak_close - 1) * 100
    benchmark = cfg.history.stock_benchmark
    if (
        asset is not None
        and asset.kind == "crypto"
        and drawdown.asset != cfg.history.crypto_benchmark
    ):
        benchmark = cfg.history.crypto_benchmark
    if benchmark in cfg.symbols():
        series = [(b.date, b.close) for b in store.read_bars(benchmark)]
    else:
        series = [(o.date, o.value) for o in store.read_macro(benchmark)]
    bench_change = _change(series, drawdown.start, drawdown.trough)
    return MarketContext(
        benchmark=benchmark,
        asset_change_pct=round(asset_change, 2),
        benchmark_change_pct=None if bench_change is None else round(bench_change, 2),
        label=classify_context(asset_change, bench_change),
    )
