"""Daily price adapters.

* **Coinbase Exchange** public candles (crypto, long history, no key).
* **CoinGecko** public API (crypto, recent ~365 days, optional demo key via
  ``COINGECKO_DEMO_API_KEY``). Close-only: CoinGecko's daily ``market_chart`` gives one price
  per day, so open/high/low are set equal to the close and the source is labelled
  ``coingecko:close-only``.
* **Stooq** CSV download (stocks, default). As of 2026-11 Stooq answers scripted requests with a
  JavaScript bot check; the adapter detects that and raises :class:`SourceBlockedError`.
* **Yahoo Finance chart API** (stocks; *unofficial*, undocumented, only used when
  ``allow_unofficial`` is on). Uses split-adjusted closes.

All prices are split-adjusted where the source supports it, so long histories have no fake
"drawdowns" at stock splits.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from proofcast.adapters.base import Clock, complete_bars, env_value, utcnow
from proofcast.models import Asset, PriceBar, bar_available_at
from proofcast.net.http import HttpClient, HttpError, SourceBlockedError

COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
STOOQ_URL = "https://stooq.com/q/d/l/"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

COINBASE_MAX_CANDLES = 300
DAY = 86400


class PriceSourceError(RuntimeError):
    """A price source could not deliver data for an asset."""


def _bar(
    asset: Asset,
    day: date,
    o: float,
    h: float,
    lo: float,
    c: float,
    v: float,
    source: str,
    fetched_at: datetime,
) -> PriceBar:
    # Some sources report highs/lows a hair inside open/close because of rounding.
    high = max(h, o, c)
    low = min(lo, o, c)
    return PriceBar(
        asset=asset.symbol,
        date=day,
        open=o,
        high=high,
        low=low,
        close=c,
        volume=max(v, 0.0),
        source=source,
        fetched_at=fetched_at,
        available_at=bar_available_at(day, asset.kind),
    )


# ---------------------------------------------------------------------------
# Coinbase Exchange
# ---------------------------------------------------------------------------


def parse_coinbase_candles(asset: Asset, payload: Any, fetched_at: datetime) -> list[PriceBar]:
    """Parse ``[[time, low, high, open, close, volume], ...]`` (newest first)."""
    if not isinstance(payload, list):
        raise PriceSourceError(f"coinbase: unexpected payload for {asset.symbol}")
    bars = []
    for row in payload:
        if not (isinstance(row, list) and len(row) >= 6):
            continue
        ts, low, high, open_, close, volume = (float(x) for x in row[:6])
        day = datetime.fromtimestamp(ts, UTC).date()
        bars.append(_bar(asset, day, open_, high, low, close, volume, "coinbase", fetched_at))
    return bars


def fetch_coinbase(
    client: HttpClient, asset: Asset, start: date, end: date | None = None, *, clock: Clock = utcnow
) -> list[PriceBar]:
    """Daily candles from ``start`` to ``end`` (inclusive), paginated 300 days per request."""
    if not asset.coinbase_product:
        raise PriceSourceError(f"{asset.symbol} has no coinbase_product")
    now = clock()
    end = end or now.date()
    out: dict[date, PriceBar] = {}
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=COINBASE_MAX_CANDLES - 1), end)
        payload = client.get_json(
            COINBASE_URL.format(product=asset.coinbase_product),
            params={
                "granularity": DAY,
                "start": f"{window_start.isoformat()}T00:00:00Z",
                "end": f"{window_end.isoformat()}T00:00:00Z",
            },
        )
        for b in parse_coinbase_candles(asset, payload, now):
            out[b.date] = b
        window_start = window_end + timedelta(days=1)
    return complete_bars(out.values(), now)


# ---------------------------------------------------------------------------
# CoinGecko
# ---------------------------------------------------------------------------


def parse_coingecko_market_chart(
    asset: Asset, payload: Any, fetched_at: datetime
) -> list[PriceBar]:
    """Parse ``market_chart?interval=daily``.

    Each daily point is stamped 00:00 UTC and is the price at that instant, i.e. the close of
    the previous UTC day. The last point is usually "now" (not at midnight) and is skipped.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("prices"), list):
        raise PriceSourceError(f"coingecko: unexpected payload for {asset.symbol}")
    volumes = {
        int(p[0]): float(p[1])
        for p in payload.get("total_volumes", [])
        if isinstance(p, list) and len(p) >= 2 and p[1] is not None
    }
    bars = []
    for point in payload["prices"]:
        if not (isinstance(point, list) and len(point) >= 2) or point[1] is None:
            continue
        ms = int(point[0])
        if ms % (DAY * 1000) != 0:
            continue
        day = datetime.fromtimestamp(ms / 1000, UTC).date() - timedelta(days=1)
        price = float(point[1])
        bars.append(
            _bar(
                asset,
                day,
                price,
                price,
                price,
                price,
                volumes.get(ms, 0.0),
                "coingecko:close-only",
                fetched_at,
            )
        )
    return bars


def fetch_coingecko(
    client: HttpClient,
    asset: Asset,
    days: int = 365,
    *,
    clock: Clock = utcnow,
    environ: dict[str, str] | None = None,
) -> list[PriceBar]:
    """Up to ``days`` (≤365 on the public API) of daily closes."""
    if not asset.coingecko_id:
        raise PriceSourceError(f"{asset.symbol} has no coingecko_id")
    headers = {}
    key = env_value(environ, "COINGECKO_DEMO_API_KEY")
    if key:
        headers["x-cg-demo-api-key"] = key
    now = clock()
    payload = client.get_json(
        COINGECKO_URL.format(coin=asset.coingecko_id),
        params={"vs_currency": "usd", "days": min(days, 365), "interval": "daily"},
        headers=headers,
    )
    return complete_bars(parse_coingecko_market_chart(asset, payload, now), now)


# ---------------------------------------------------------------------------
# Stooq
# ---------------------------------------------------------------------------


def parse_stooq_csv(asset: Asset, text: str, fetched_at: datetime) -> list[PriceBar]:
    """Parse Stooq's ``Date,Open,High,Low,Close,Volume`` CSV."""
    stripped = text.lstrip()
    if stripped.startswith("<") or "javascript" in stripped[:500].lower():
        raise SourceBlockedError("stooq returned an HTML page (bot check), not CSV")
    if not stripped or stripped.lower().startswith("no data"):
        raise PriceSourceError(f"stooq: no data for {asset.symbol}")
    reader = csv.DictReader(io.StringIO(stripped))
    if reader.fieldnames is None or "Close" not in reader.fieldnames:
        raise PriceSourceError(f"stooq: unexpected CSV header for {asset.symbol}")
    bars = []
    for row in reader:
        try:
            day = date.fromisoformat(row["Date"])
            o, h, lo, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
            volume = float(row.get("Volume") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        if min(o, h, lo, c) <= 0:
            continue
        bars.append(_bar(asset, day, o, h, lo, c, volume, "stooq", fetched_at))
    return bars


def fetch_stooq(
    client: HttpClient, asset: Asset, start: date, end: date | None = None, *, clock: Clock = utcnow
) -> list[PriceBar]:
    if not asset.stooq_symbol:
        raise PriceSourceError(f"{asset.symbol} has no stooq_symbol")
    now = clock()
    end = end or now.date()
    text = client.get_text(
        STOOQ_URL,
        params={
            "s": asset.stooq_symbol,
            "i": "d",
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
        },
    )
    return complete_bars(parse_stooq_csv(asset, text, now), now)


# ---------------------------------------------------------------------------
# Yahoo Finance (unofficial)
# ---------------------------------------------------------------------------


def parse_yahoo_chart(asset: Asset, payload: Any, fetched_at: datetime) -> list[PriceBar]:
    """Parse ``/v8/finance/chart``. Closes are split-adjusted (not dividend-adjusted)."""
    try:
        chart = payload["chart"]
        if chart.get("error"):
            raise PriceSourceError(f"yahoo: {chart['error']}")
        result = chart["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        offset = int(result.get("meta", {}).get("gmtoffset", 0))
    except (KeyError, IndexError, TypeError) as exc:
        raise PriceSourceError(f"yahoo: unexpected payload for {asset.symbol}") from exc
    bars = []
    for i, ts in enumerate(timestamps):
        try:
            o, h, lo, c = (quote[k][i] for k in ("open", "high", "low", "close"))
            v = quote.get("volume", [0] * len(timestamps))[i] or 0
        except (KeyError, IndexError):
            continue
        if None in (o, h, lo, c) or min(o, h, lo, c) <= 0:
            continue
        day = datetime.fromtimestamp(int(ts) + offset, UTC).date()
        source = "yahoo-unofficial"
        bars.append(
            _bar(asset, day, float(o), float(h), float(lo), float(c), float(v), source, fetched_at)
        )
    return bars


def fetch_yahoo(
    client: HttpClient, asset: Asset, start: date, end: date | None = None, *, clock: Clock = utcnow
) -> list[PriceBar]:
    if not asset.yahoo_symbol:
        raise PriceSourceError(f"{asset.symbol} has no yahoo_symbol")
    now = clock()
    end = end or now.date()
    period1 = int(datetime.combine(start, datetime.min.time(), UTC).timestamp())
    period2 = int(datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC).timestamp())
    payload = client.get_json(
        YAHOO_URL.format(symbol=asset.yahoo_symbol),
        params={"period1": period1, "period2": period2, "interval": "1d", "events": "split"},
    )
    return complete_bars(parse_yahoo_chart(asset, payload, now), now)


# ---------------------------------------------------------------------------
# Source chain
# ---------------------------------------------------------------------------

Fetcher = Callable[[HttpClient, Asset, date], list[PriceBar]]


def fetch_prices(
    client: HttpClient,
    asset: Asset,
    start: date,
    providers: list[str],
    *,
    allow_unofficial: bool = False,
    clock: Clock = utcnow,
    environ: dict[str, str] | None = None,
) -> tuple[list[PriceBar], list[str]]:
    """Try ``providers`` in order; return the first non-empty result and the errors seen."""
    errors: list[str] = []
    for name in providers:
        try:
            if name == "coinbase":
                bars = fetch_coinbase(client, asset, start, clock=clock)
            elif name == "coingecko":
                days = max(1, (clock().date() - start).days + 1)
                bars = fetch_coingecko(client, asset, days, clock=clock, environ=environ)
            elif name == "stooq":
                bars = fetch_stooq(client, asset, start, clock=clock)
            elif name == "yahoo":
                if not allow_unofficial:
                    errors.append("yahoo: skipped (unofficial source not allowed)")
                    continue
                bars = fetch_yahoo(client, asset, start, clock=clock)
            else:
                errors.append(f"{name}: unknown provider")
                continue
        except (HttpError, PriceSourceError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        bars = [b for b in bars if b.date >= start]
        if bars:
            return bars, errors
        errors.append(f"{name}: no bars")
    return [], errors
