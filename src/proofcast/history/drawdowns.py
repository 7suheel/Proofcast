"""Drawdown detection over daily closes.

An *episode* starts at a running peak close and ends at the first later close that is back at
or above that peak (the recovery). The trough is the lowest close inside the episode. An
episode is reported as a drawdown when ``(peak - trough) / peak`` is at least the threshold.
The last episode may be unrecovered (``recovery`` is None).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from proofcast.models import AssetKind, Drawdown, PriceBar


def default_threshold(kind: AssetKind, stock: float = 0.20, crypto: float = 0.35) -> float:
    return crypto if kind == "crypto" else stock


def detect_drawdowns(
    bars: Sequence[PriceBar], threshold: float, *, since: date | None = None
) -> list[Drawdown]:
    """Every peak-to-trough fall deeper than ``threshold`` (0.2 = 20%), oldest first."""
    if not 0 < threshold < 1:
        raise ValueError("threshold must be between 0 and 1")
    closes = [(b.date, b.close) for b in sorted(bars, key=lambda b: b.date)]
    if since is not None:
        closes = [c for c in closes if c[0] >= since]
    if not closes:
        return []
    asset = bars[0].asset
    out: list[Drawdown] = []
    peak_date, peak = closes[0]
    trough_date, trough = closes[0]
    for day, close in closes[1:]:
        if close >= peak:
            _maybe_add(out, asset, peak_date, peak, trough_date, trough, day, threshold)
            peak_date, peak = day, close
            trough_date, trough = day, close
        elif close < trough:
            trough_date, trough = day, close
    _maybe_add(out, asset, peak_date, peak, trough_date, trough, None, threshold)
    return out


def _maybe_add(
    out: list[Drawdown],
    asset: str,
    peak_date: date,
    peak: float,
    trough_date: date,
    trough: float,
    recovery: date | None,
    threshold: float,
) -> None:
    depth = (peak - trough) / peak
    if depth >= threshold and trough_date > peak_date:
        out.append(
            Drawdown(
                asset=asset,
                start=peak_date,
                trough=trough_date,
                recovery=recovery,
                peak_close=peak,
                trough_close=trough,
                depth_pct=round(depth * 100, 2),
            )
        )


def find_drawdown(drawdowns: Sequence[Drawdown], day: date) -> Drawdown | None:
    """The drawdown whose episode contains ``day`` (start to recovery), else the nearest one
    whose trough is within 30 days of ``day``."""
    for d in drawdowns:
        end = d.recovery or date.max
        if d.start <= day <= end:
            return d
    near = [d for d in drawdowns if abs((d.trough - day).days) <= 30]
    return min(near, key=lambda d: abs((d.trough - day).days)) if near else None
