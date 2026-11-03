"""Questions, base rates and simple baselines (no AI).

**Question.** For a forecast made at time T, the *reference bar* is the last daily bar that was
complete before T. The question asks whether the close of the first bar dated on or after
``reference_date + horizon_days`` is higher than the reference close.

**Base rate.** Over the price history available before T (at most the last
``lookback_years``), the share of days whose close was below the close of the first bar on or
after ``day + horizon``, with Laplace smoothing ``(k + 1) / (n + 2)``. Only outcomes already
known before T are counted.

**Momentum baseline.** ``base_rate + 0.10 * tanh(r / sigma)``, where ``r`` is the log return over
the last ``horizon_days`` and ``sigma`` the typical size of such a return
(daily log-return standard deviation times the square root of the number of bars in a
horizon window). Clamped to [0.05, 0.95].

**Always 50%.** The coin flip every forecaster should beat.
"""

from __future__ import annotations

import itertools
import math
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import pstdev

from proofcast.models import PriceBar, ResolutionRule

MOMENTUM_STRENGTH = 0.10


@dataclass(frozen=True)
class Baselines:
    always_50: float
    base_rate: float
    momentum: float
    base_rate_n: int
    """How many past outcomes the base rate was computed from."""

    def as_dict(self) -> dict[str, float]:
        return {"always_50": self.always_50, "base_rate": self.base_rate, "momentum": self.momentum}


def clamp(p: float, lo: float = 0.05, hi: float = 0.95) -> float:
    return min(hi, max(lo, p))


def reference_bar(bars: Sequence[PriceBar], as_of: datetime) -> PriceBar | None:
    """The last bar that was complete strictly before ``as_of``."""
    known = [b for b in bars if b.available_at < as_of]
    return max(known, key=lambda b: b.date) if known else None


def make_rule(ref: PriceBar, horizon_days: int) -> ResolutionRule:
    return ResolutionRule(
        asset=ref.asset,
        reference_date=ref.date,
        reference_close=ref.close,
        target_date=ref.date + timedelta(days=horizon_days),
    )


def question_text(rule: ResolutionRule) -> str:
    return (
        f"Will {rule.asset} close above {rule.reference_close:g} (its close on "
        f"{rule.reference_date.isoformat()}) on the first trading day on or after "
        f"{rule.target_date.isoformat()}?"
    )


def base_rate(
    bars: Sequence[PriceBar], horizon_days: int, as_of: datetime, *, lookback_years: int = 10
) -> tuple[float, int]:
    """Smoothed historical frequency of "higher after ``horizon_days``" known before ``as_of``."""
    known = sorted((b for b in bars if b.available_at < as_of), key=lambda b: b.date)
    if not known:
        return 0.5, 0
    start = known[-1].date - timedelta(days=round(365.25 * lookback_years))
    known = [b for b in known if b.date >= start]
    dates = [b.date for b in known]
    ups = n = 0
    for i, b in enumerate(known):
        j = bisect_left(dates, b.date + timedelta(days=horizon_days), lo=i + 1)
        if j >= len(known):
            break
        n += 1
        ups += known[j].close > b.close
    return (ups + 1) / (n + 2), n


def _bars_since(known: Sequence[PriceBar], day: date) -> list[PriceBar]:
    return [b for b in known if b.date >= day]


def momentum(bars: Sequence[PriceBar], horizon_days: int, as_of: datetime, rate: float) -> float:
    known = sorted((b for b in bars if b.available_at < as_of), key=lambda b: b.date)
    if len(known) < 3:
        return clamp(rate)
    last = known[-1]
    window = _bars_since(known, last.date - timedelta(days=horizon_days))
    start_bar = window[0] if window else known[0]
    if start_bar.date == last.date:
        return clamp(rate)
    r = math.log(last.close / start_bar.close)
    recent = known[-min(len(known), 252) :]
    logs = [math.log(b.close / a.close) for a, b in itertools.pairwise(recent)]
    daily_sd = pstdev(logs) if len(logs) > 1 else 0.0
    sigma = daily_sd * math.sqrt(max(1, len(window) - 1))
    if sigma <= 0:
        return clamp(rate)
    return clamp(rate + MOMENTUM_STRENGTH * math.tanh(r / sigma))


def compute_baselines(
    bars: Sequence[PriceBar], horizon_days: int, as_of: datetime, *, lookback_years: int = 10
) -> Baselines:
    rate, n = base_rate(bars, horizon_days, as_of, lookback_years=lookback_years)
    return Baselines(
        always_50=0.5,
        base_rate=round(rate, 4),
        momentum=round(momentum(bars, horizon_days, as_of, rate), 4),
        base_rate_n=n,
    )
