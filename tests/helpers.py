"""Small builders for test data. No network, ever."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from proofcast.models import (
    AssetKind,
    Filing,
    MacroObservation,
    NewsItem,
    PriceBar,
    bar_available_at,
)

T0 = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
FETCHED = datetime(2024, 6, 1, tzinfo=UTC)


def bar(
    day: date,
    close: float,
    *,
    asset: str = "AAPL",
    kind: AssetKind = "stock",
    source: str = "test",
) -> PriceBar:
    return PriceBar(
        asset=asset,
        date=day,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000,
        source=source,
        fetched_at=FETCHED,
        available_at=bar_available_at(day, kind),
    )


def series(
    closes: list[float],
    *,
    start: date = date(2020, 1, 1),
    asset: str = "AAPL",
    kind: AssetKind = "stock",
) -> list[PriceBar]:
    """One bar per calendar day starting at ``start``."""
    return [bar(start + timedelta(days=i), c, asset=asset, kind=kind) for i, c in enumerate(closes)]


def news(
    title: str,
    published_at: datetime,
    *,
    assets: list[str] | None = None,
    url: str | None = None,
    summary: str | None = "summary",
    source: str = "test-feed",
) -> NewsItem:
    if url is None:
        digest = hashlib.sha256(f"{title}{published_at.isoformat()}".encode()).hexdigest()[:10]
        url = f"https://example.com/{digest}"
    return NewsItem(
        id=NewsItem.make_id(url, title),
        title=title,
        url=url,
        source=source,
        published_at=published_at,
        fetched_at=FETCHED,
        assets=assets if assets is not None else ["AAPL"],
        summary=summary,
    )


def filing(
    accession: str,
    filed_at: datetime,
    *,
    company: str = "AAPL",
    form_type: str = "8-K",
    items: list[str] | None = None,
) -> Filing:
    return Filing(
        id=accession,
        company=company,
        cik="0000320193",
        form_type=form_type,
        filed_at=filed_at,
        url=f"https://www.sec.gov/Archives/edgar/data/320193/{accession}.htm",
        items=items or [],
        fetched_at=FETCHED,
    )


def macro(
    series_id: str, day: date, value: float, published_at: datetime, *, estimated: bool = True
) -> MacroObservation:
    return MacroObservation(
        series_id=series_id,
        date=day,
        value=value,
        published_at=published_at,
        published_at_estimated=estimated,
        source="test",
        fetched_at=FETCHED,
    )
