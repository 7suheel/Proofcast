"""Scoring and the track record.

For resolved forecasts with probability ``p`` and outcome ``o`` (1 = closed higher):

* **Brier score** ``mean((p - o)^2)``. Lower is better; always saying 50% scores 0.25.
* **Log score** ``mean(ln(p if o else 1 - p))``. Higher (closer to 0) is better.
* **Hit rate** at 50%: share on the right side of 50%; exactly 50% counts as half a hit.
* **Calibration**: ten buckets [0, 0.1), ..., [0.9, 1.0]; in each, the mean forecast and the
  share that resolved YES. A well-calibrated forecaster's points lie on the diagonal.

Baselines are scored on **the same questions** as each setup: always 50%, the historical base
rate, and momentum, using the baseline probabilities stored on each forecast. The track record
says plainly when a setup is not better than every baseline, and when there are too few
resolved forecasts to tell.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from proofcast.models import CalibrationBucket, Forecast, ForecastResolution, ScoreSummary

BASELINE_NAMES = ("always_50", "base_rate", "momentum")
MIN_FOR_VERDICT = 30
_EPS = 1e-6


def brier(pairs: Sequence[tuple[float, bool]]) -> float | None:
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def log_score(pairs: Sequence[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for p, o in pairs:
        q = min(1 - _EPS, max(_EPS, p if o else 1 - p))
        total += math.log(q)
    return total / len(pairs)


def hit_rate(pairs: Sequence[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    hits = sum(0.5 if p == 0.5 else float((p > 0.5) == o) for p, o in pairs)
    return hits / len(pairs)


def calibration(pairs: Sequence[tuple[float, bool]]) -> list[CalibrationBucket]:
    buckets = []
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        inside = [(p, o) for p, o in pairs if lo <= p < hi or (i == 9 and p == 1.0)]
        buckets.append(
            CalibrationBucket(
                lower=lo,
                upper=hi,
                n=len(inside),
                mean_probability=sum(p for p, _ in inside) / len(inside) if inside else None,
                observed_frequency=sum(o for _, o in inside) / len(inside) if inside else None,
            )
        )
    return buckets


def summarize(
    group: str,
    setup: str,
    forecasts: Sequence[Forecast],
    resolutions: dict[str, ForecastResolution],
    *,
    probability_of: str | None = None,
    asset: str | None = None,
    horizon: int | None = None,
) -> ScoreSummary:
    """Score ``forecasts``; ``probability_of`` picks a stored baseline instead of the forecast."""
    pairs: list[tuple[float, bool]] = []
    for f in forecasts:
        r = resolutions.get(f.id)
        if r is None or r.status != "resolved" or r.outcome is None:
            continue
        p = f.probability if probability_of is None else f.baselines.get(probability_of)
        if p is None:
            continue
        pairs.append((p, r.outcome))
    b, ls, hr = brier(pairs), log_score(pairs), hit_rate(pairs)
    return ScoreSummary(
        group=group,
        setup=setup,
        asset=asset,
        horizon_days=horizon,
        n_forecasts=len(forecasts),
        n_resolved=len(pairs),
        brier=None if b is None else round(b, 5),
        log_score=None if ls is None else round(ls, 5),
        hit_rate=None if hr is None else round(hr, 4),
        calibration=calibration(pairs),
    )


class SetupRecord(BaseModel):
    """One forecasting setup next to the baselines scored on the same questions."""

    model_config = ConfigDict(extra="forbid")
    setup: str
    is_baseline_setup: bool
    overall: ScoreSummary
    baselines: dict[str, ScoreSummary]
    by_asset: list[ScoreSummary] = Field(default_factory=list)
    by_horizon: list[ScoreSummary] = Field(default_factory=list)
    verdict: str


class TrackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: datetime
    n_forecasts: int
    n_resolved: int
    n_void: int
    setups: list[SetupRecord]


def verdict(overall: ScoreSummary, baselines: dict[str, ScoreSummary]) -> str:
    if overall.n_resolved == 0 or overall.brier is None:
        return "No resolved forecasts yet: nothing can be said about accuracy."
    scored = {k: v.brier for k, v in baselines.items() if v.brier is not None}
    worse_or_equal = [k for k, v in scored.items() if overall.brier >= v]
    prefix = ""
    if overall.n_resolved < MIN_FOR_VERDICT:
        prefix = f"Only {overall.n_resolved} resolved forecasts: too few to tell skill from luck. "
    if worse_or_equal:
        names = ", ".join(n.replace("_", " ") for n in sorted(worse_or_equal))
        return prefix + f"NOT better than the baseline(s): {names} (Brier score, lower is better)."
    return prefix + "Better than all three baselines on Brier score so far."


def track_record(
    forecasts: Iterable[Forecast],
    resolutions: dict[str, ForecastResolution],
    *,
    now: datetime,
) -> TrackRecord:
    forecasts = list(forecasts)
    by_setup: dict[str, list[Forecast]] = defaultdict(list)
    for f in forecasts:
        by_setup[f.setup].append(f)
    setups: list[SetupRecord] = []
    for setup, fs in sorted(by_setup.items()):
        overall = summarize(f"{setup}", setup, fs, resolutions)
        bases = {
            name: summarize(
                f"{setup} vs {name}", f"baseline:{name}", fs, resolutions, probability_of=name
            )
            for name in BASELINE_NAMES
        }
        by_asset = [
            summarize(
                f"{setup} / {a}", setup, [f for f in fs if f.asset == a], resolutions, asset=a
            )
            for a in sorted({f.asset for f in fs})
        ]
        by_horizon = [
            summarize(
                f"{setup} / {h}d",
                setup,
                [f for f in fs if f.horizon_days == h],
                resolutions,
                horizon=h,
            )
            for h in sorted({f.horizon_days for f in fs})
        ]
        setups.append(
            SetupRecord(
                setup=setup,
                is_baseline_setup=all(f.is_baseline for f in fs),
                overall=overall,
                baselines=bases,
                by_asset=by_asset,
                by_horizon=by_horizon,
                verdict=verdict(overall, bases),
            )
        )
    statuses = [resolutions[f.id].status for f in forecasts if f.id in resolutions]
    return TrackRecord(
        generated_at=now,
        n_forecasts=len(forecasts),
        n_resolved=statuses.count("resolved"),
        n_void=statuses.count("void"),
        setups=setups,
    )


def track_record_text(record: TrackRecord) -> str:
    def fmt(v: float | None, spec: str = ".4f") -> str:
        return "-" if v is None else format(v, spec)

    lines = [
        f"Track record: {record.n_forecasts} forecasts, {record.n_resolved} resolved, "
        f"{record.n_void} void",
    ]
    for s in record.setups:
        lines += ["", f"Setup {s.setup}" + (" (baseline setup)" if s.is_baseline_setup else "")]
        lines.append(f"  {'':<22}{'n':>6}{'Brier':>9}{'log':>9}{'hit':>8}")
        rows = [("this setup", s.overall)] + [(f"vs {k}", v) for k, v in s.baselines.items()]
        for name, sc in rows:
            lines.append(
                f"  {name:<22}{sc.n_resolved:>6}{fmt(sc.brier):>9}{fmt(sc.log_score):>9}"
                f"{fmt(sc.hit_rate, '.1%'):>8}"
            )
        lines.append(f"  {s.verdict}")
    lines += [
        "",
        "Lower Brier is better; always saying 50% scores 0.25.",
        "Not investment advice. Forecasts can be wrong. "
        "Past performance does not predict future results.",
    ]
    return "\n".join(lines)
