"""Point-in-time data access (hard rule 4: no lookahead).

:func:`get_data` returns only items that were public strictly before ``as_of``:

* price bars with ``available_at < as_of``,
* news with ``published_at < as_of``,
* filings with ``filed_at < as_of``,
* macro observations with ``published_at < as_of``.

:func:`assert_no_lookahead` is the second line of defence: the agents' context builder calls
it on everything it is about to show a model, and it raises :class:`LookaheadError` if any item
is not strictly before the forecast time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from proofcast.models import (
    Filing,
    FinancialFact,
    MacroObservation,
    NewsItem,
    PriceBar,
    SourceRef,
)
from proofcast.storage import DataStore

PublicItem = PriceBar | NewsItem | Filing | MacroObservation | SourceRef | FinancialFact


class LookaheadError(RuntimeError):
    """Raised when data published at or after the forecast time reaches a forecast."""


@dataclass(frozen=True)
class Snapshot:
    """Everything known about one asset at time ``as_of``."""

    asset: str
    as_of: datetime
    bars: list[PriceBar] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    filings: list[Filing] = field(default_factory=list)
    macro: dict[str, list[MacroObservation]] = field(default_factory=dict)

    def latest_bar(self) -> PriceBar | None:
        return self.bars[-1] if self.bars else None


def _check_as_of(as_of: datetime) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")


def filter_bars(bars: Iterable[PriceBar], as_of: datetime) -> list[PriceBar]:
    _check_as_of(as_of)
    return sorted((b for b in bars if b.available_at < as_of), key=lambda b: b.date)


def filter_news(items: Iterable[NewsItem], as_of: datetime) -> list[NewsItem]:
    _check_as_of(as_of)
    return sorted((n for n in items if n.published_at < as_of), key=lambda n: n.published_at)


def filter_filings(items: Iterable[Filing], as_of: datetime) -> list[Filing]:
    _check_as_of(as_of)
    return sorted((f for f in items if f.filed_at < as_of), key=lambda f: f.filed_at)


def filter_macro(items: Iterable[MacroObservation], as_of: datetime) -> list[MacroObservation]:
    _check_as_of(as_of)
    return sorted((m for m in items if m.published_at < as_of), key=lambda m: m.date)


def get_data(
    store: DataStore,
    asset: str,
    as_of: datetime,
    *,
    macro_series: Iterable[str] = (),
    news_lookback_days: int | None = None,
) -> Snapshot:
    """Return what was public about ``asset`` strictly before ``as_of``.

    News and filings are limited to items tagged with / filed by ``asset``. If
    ``news_lookback_days`` is set, older news is left out (it was still public, just less useful).
    """
    _check_as_of(as_of)
    bars = filter_bars(store.read_bars(asset), as_of)
    news = [n for n in filter_news(store.read_news(), as_of) if asset in n.assets]
    if news_lookback_days is not None:
        cutoff = as_of.timestamp() - news_lookback_days * 86400
        news = [n for n in news if n.published_at.timestamp() >= cutoff]
    filings = [f for f in filter_filings(store.read_filings(), as_of) if f.company == asset]
    macro = {sid: filter_macro(store.read_macro(sid), as_of) for sid in macro_series}
    snapshot = Snapshot(
        asset=asset, as_of=as_of, bars=bars, news=news, filings=filings, macro=macro
    )
    assert_snapshot_clean(snapshot)
    return snapshot


def item_time(item: PublicItem) -> datetime:
    """The time an item became public."""
    if isinstance(item, PriceBar):
        return item.available_at
    if isinstance(item, Filing):
        return item.filed_at
    return item.published_at


def assert_no_lookahead(
    items: Iterable[PublicItem],
    as_of: datetime,
) -> None:
    """Raise :class:`LookaheadError` if any item was not public strictly before ``as_of``."""
    _check_as_of(as_of)
    for item in items:
        t = item_time(item)
        if t >= as_of:
            label = getattr(item, "id", None) or getattr(item, "date", "?")
            raise LookaheadError(
                f"{type(item).__name__} {label} became public at {t.isoformat()}, "
                f"not before {as_of.isoformat()}"
            )


def assert_snapshot_clean(snapshot: Snapshot) -> None:
    """Check every item in ``snapshot`` against its ``as_of``."""
    assert_no_lookahead(snapshot.bars, snapshot.as_of)
    assert_no_lookahead(snapshot.news, snapshot.as_of)
    assert_no_lookahead(snapshot.filings, snapshot.as_of)
    for series in snapshot.macro.values():
        assert_no_lookahead(series, snapshot.as_of)
