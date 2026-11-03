"""Technical indicators computed by our code from point-in-time bars.

The Technical agent only ever sees these numbers; it is told never to invent others.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean, pstdev

from proofcast.models import AssetKind, PriceBar


@dataclass(frozen=True)
class Indicator:
    id: str
    name: str
    value: float
    unit: str

    def text(self) -> str:
        return (
            f"{self.name}: {self.value:+.2f}{self.unit}"
            if self.unit == "%"
            else (f"{self.name}: {self.value:.2f}{self.unit}")
        )


def _ret(bars: Sequence[PriceBar], days: int) -> float | None:
    last = bars[-1]
    cutoff = last.date - timedelta(days=days)
    earlier = [b for b in bars if b.date <= cutoff]
    if not earlier:
        return None
    return (last.close / earlier[-1].close - 1) * 100


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) <= period:
        return None
    gains = [max(0.0, b - a) for a, b in itertools.pairwise(closes)]
    losses = [max(0.0, a - b) for a, b in itertools.pairwise(closes)]
    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])
    for g, lo in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + lo) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def compute_indicators(bars: Sequence[PriceBar], kind: AssetKind) -> list[Indicator]:
    """Indicators from bars that are already filtered to what was known at forecast time."""
    bars = sorted(bars, key=lambda b: b.date)
    if len(bars) < 2:
        return []
    closes = [b.close for b in bars]
    out: list[Indicator] = []

    def add(key: str, name: str, value: float | None, unit: str = "%") -> None:
        if value is not None and math.isfinite(value):
            out.append(Indicator(f"I:{key}", name, round(value, 4), unit))

    for days, label in ((7, "7d"), (30, "30d"), (90, "90d"), (365, "1y")):
        add(f"ret_{label}", f"Return over {label}", _ret(bars, days))
    periods = 365 if kind == "crypto" else 252
    recent = closes[-31:]
    logs = [math.log(b / a) for a, b in itertools.pairwise(recent)]
    if len(logs) > 2:
        add(
            "vol_30d",
            "Annualised volatility (last 30 bars)",
            pstdev(logs) * math.sqrt(periods) * 100,
        )
    year = [b.close for b in bars if b.date > bars[-1].date - timedelta(days=365)]
    add("from_52w_high", "Distance from 52-week high close", (closes[-1] / max(year) - 1) * 100)
    for n in (50, 200):
        if len(closes) >= n:
            add(
                f"vs_sma{n}",
                f"Close vs {n}-bar average",
                (closes[-1] / mean(closes[-n:]) - 1) * 100,
            )
    add("rsi14", "RSI (14 bars)", rsi(closes), "")
    return out
