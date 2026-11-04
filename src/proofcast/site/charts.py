"""Inline SVG charts rendered at build time.

The site ships no JavaScript: charts are SVG strings with CSS classes, so they work offline, on
phones and in dark mode (colours come from CSS variables). All text is escaped.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from html import escape

from proofcast.models import CalibrationBucket, Drawdown, PriceBar

MAX_POINTS = 700
LOG_SCALE_RATIO = 10


def _downsample(
    points: Sequence[tuple[date, float]], limit: int = MAX_POINTS
) -> list[tuple[date, float]]:
    if len(points) <= limit:
        return list(points)
    step = len(points) / limit
    out = [points[int(i * step)] for i in range(limit)]
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def axis_label(v: float) -> str:
    """Compact axis labels: 0.512, 61.2, 1,234, 125k, 1.2M."""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 100:
        return f"{v:,.0f}"
    return f"{v:.3g}"


def price_chart(
    bars: Sequence[PriceBar],
    drawdowns: Sequence[Drawdown] = (),
    *,
    width: int = 800,
    height: int = 260,
    label: str = "",
) -> str:
    """Daily closes as a line, with each drawdown (peak to trough) shaded."""
    points = _downsample([(b.date, b.close) for b in sorted(bars, key=lambda b: b.date)])
    if len(points) < 2:
        return '<p class="muted">Not enough price data for a chart yet.</p>'
    pad_l, pad_r, pad_t, pad_b = 78, 10, 12, 30
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    d0, d1 = points[0][0], points[-1][0]
    span = max(1, (d1 - d0).days)
    lo = min(v for _, v in points)
    hi = max(v for _, v in points)
    # Long histories that grew more than 10x read better on a log scale.
    log_scale = lo > 0 and hi / lo > LOG_SCALE_RATIO
    t_lo, t_hi = (math.log(lo), math.log(hi)) if log_scale else (lo, hi)
    rng = (t_hi - t_lo) or 1.0

    def x(d: date) -> float:
        return pad_l + (d - d0).days / span * w

    def y(v: float) -> float:
        t = math.log(v) if log_scale else v
        return pad_t + (1 - (t - t_lo) / rng) * h

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{escape(label or "Price chart")}" xmlns="http://www.w3.org/2000/svg">'
    ]
    for d in drawdowns:
        if d.trough < d0 or d.start > d1:
            continue
        x0, x1 = x(max(d.start, d0)), x(min(d.trough, d1))
        parts.append(
            f'<rect class="dd" x="{x0:.1f}" y="{pad_t}" width="{max(1.0, x1 - x0):.1f}" '
            f'height="{h}"><title>{escape(f"-{d.depth_pct:.1f}% from {d.start} to {d.trough}")}'
            "</title></rect>"
        )
    for frac in (0.0, 0.5, 1.0):
        t = t_lo + frac * rng
        v = math.exp(t) if log_scale else t
        yy = y(v)
        parts.append(
            f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_l - 6}" y="{yy + 4:.1f}" text-anchor="end">'
            f"{axis_label(v)}</text>"
        )
    for year in range(d0.year + 1, d1.year + 1):
        xx = x(date(year, 1, 1))
        parts.append(
            f'<text class="axis" x="{xx:.1f}" y="{height - 4}" text-anchor="middle">{year}</text>'
        )
    path = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in points)
    parts.append(f'<polyline class="line" fill="none" points="{path}"/>')
    if log_scale:
        parts.append(f'<text class="axis" x="{pad_l + 6}" y="{pad_t + 16}">log scale</text>')
    parts.append("</svg>")
    return "".join(parts)


def calibration_chart(series: Mapping[str, Sequence[CalibrationBucket]], *, size: int = 320) -> str:
    """Forecast probability (x) vs observed frequency (y) per bucket; the diagonal is perfect."""
    pad = 36
    inner = size - 2 * pad

    def sx(p: float) -> float:
        return pad + p * inner

    def sy(p: float) -> float:
        return pad + (1 - p) * inner

    parts = [
        f'<svg class="chart calib" viewBox="0 0 {size} {size}" role="img" '
        'aria-label="Calibration chart" xmlns="http://www.w3.org/2000/svg">',
        f'<rect class="frame" x="{pad}" y="{pad}" width="{inner}" height="{inner}" fill="none"/>',
        f'<line class="diag" x1="{sx(0)}" y1="{sy(0)}" x2="{sx(1)}" y2="{sy(1)}"/>',
    ]
    for t in (0.0, 0.5, 1.0):
        parts.append(
            f'<text class="axis" x="{sx(t):.1f}" y="{size - 12}" text-anchor="middle">'
            f"{t:.0%}</text>"
        )
        parts.append(
            f'<text class="axis" x="{pad - 6}" y="{sy(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>'
        )
    for i, (name, buckets) in enumerate(series.items()):
        cls = f"s{i % 4}"
        pts = [
            (b.mean_probability, b.observed_frequency, b.n)
            for b in buckets
            if b.mean_probability is not None and b.observed_frequency is not None
        ]
        if len(pts) > 1:
            path = " ".join(f"{sx(p):.1f},{sy(o):.1f}" for p, o, _ in pts)
            parts.append(f'<polyline class="cal {cls}" fill="none" points="{path}"/>')
        for p, o, n in pts:
            r = min(8.0, 2.5 + n**0.5)
            parts.append(
                f'<circle class="pt {cls}" cx="{sx(p):.1f}" cy="{sy(o):.1f}" r="{r:.1f}">'
                f"<title>{escape(name)}: forecast {p:.0%}, observed {o:.0%}, n={n}</title></circle>"
            )
    parts.append("</svg>")
    return "".join(parts)
