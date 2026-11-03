"""Resolve forecasts from price data only (no AI).

For each unresolved forecast whose target date has passed:

* the *target bar* is the first stored bar dated on or after ``target_date`` that was already
  complete at resolution time;
* the outcome is ``target close > reference close``, both taken from the **current** stored
  series, so a stock split or other adjustment between the two dates cannot flip the answer.
  When the current reference close differs from the one recorded in the forecast by more than
  1%, a note says so (the recorded value stays in the ledger for audit);
* if no bar exists within ``max_wait_days`` after the target date, the forecast is resolved as
  ``void`` (it then counts in no score; void resolutions are listed on the site).

Resolutions are appended to the ledger; nothing is ever edited.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from proofcast.models import Forecast, ForecastResolution, PriceBar
from proofcast.storage import DataStore

ADJUSTMENT_TOLERANCE = 0.01


def resolution_id(forecast_id: str) -> str:
    return f"res-{forecast_id}"


def resolve_one(
    forecast: Forecast, bars: Sequence[PriceBar], now: datetime
) -> ForecastResolution | None:
    """Resolve ``forecast`` if its outcome is known at ``now``; otherwise return None."""
    rule = forecast.resolution_rule
    known = sorted((b for b in bars if b.available_at <= now), key=lambda b: b.date)
    target = next((b for b in known if b.date >= rule.target_date), None)
    common = {
        "id": resolution_id(forecast.id),
        "forecast_id": forecast.id,
        "resolved_at": now,
        "reference_close": rule.reference_close,
    }
    deadline = rule.target_date + timedelta(days=rule.max_wait_days)
    if target is None or target.date > deadline:
        if now.date() > deadline + timedelta(days=1):
            return ForecastResolution(
                **common,
                status="void",
                outcome=None,
                target_bar_date=None,
                target_close=None,
                price_source=None,
                note=f"no price bar between {rule.target_date} and {deadline}",
            )
        return None
    ref_bar = next((b for b in known if b.date == rule.reference_date), None)
    ref_close = ref_bar.close if ref_bar is not None else rule.reference_close
    note = ""
    if ref_bar is None:
        note = "reference bar no longer stored; used the recorded reference close"
    elif abs(ref_bar.close / rule.reference_close - 1) > ADJUSTMENT_TOLERANCE:
        note = (
            f"price series adjusted since the forecast (reference close now {ref_bar.close:g}, "
            f"recorded {rule.reference_close:g}); outcome uses the current series"
        )
    return ForecastResolution(
        **common,
        status="resolved",
        outcome=target.close > ref_close,
        target_bar_date=target.date,
        target_close=target.close,
        price_source=target.source,
        note=note,
    )


def resolve_due(
    forecasts: Sequence[Forecast],
    resolved_ids: set[str],
    store: DataStore,
    now: datetime,
) -> list[ForecastResolution]:
    """Resolutions for every unresolved forecast whose outcome is now known."""
    out: list[ForecastResolution] = []
    bars_cache: dict[str, list[PriceBar]] = {}
    for f in sorted(forecasts, key=lambda f: f.created_at):
        if f.id in resolved_ids or f.resolution_rule.target_date > now.date():
            continue
        bars = bars_cache.setdefault(f.asset, store.read_bars(f.asset))
        r = resolve_one(f, bars, now)
        if r is not None:
            out.append(r)
    return out
