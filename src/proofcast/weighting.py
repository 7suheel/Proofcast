"""Agent weights from each agent's own track record.

For each agent, take its probabilities on the last :data:`WINDOW` resolved forecasts it took
part in and compute its Brier score ``B``. Then::

    weight = clamp(exp(-K * (B - 0.25)), MIN_WEIGHT, MAX_WEIGHT)     if n >= MIN_RESOLVED
    weight = 1                                                       otherwise

with ``K = 10``. 0.25 is the Brier score of always saying 50%, so an agent that does worse than a
coin flip gets a weight below 1 and an agent that does better gets more, within [0.25, 2].
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from proofcast.models import Forecast, ForecastResolution

WINDOW = 50
MIN_RESOLVED = 10
K = 10.0
REFERENCE_BRIER = 0.25
MIN_WEIGHT = 0.25
MAX_WEIGHT = 2.0


def agent_brier_scores(
    forecasts: Iterable[Forecast],
    resolutions: dict[str, ForecastResolution],
    *,
    window: int = WINDOW,
) -> dict[str, tuple[float, int]]:
    """(Brier, n) per agent over its last ``window`` resolved forecasts."""
    history: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for f in sorted(forecasts, key=lambda f: f.created_at):
        r = resolutions.get(f.id)
        if r is None or r.status != "resolved" or r.outcome is None:
            continue
        for o in f.agent_opinions:
            p = o.probabilities.get(f.horizon_days)
            if p is not None:
                history[o.agent].append((p, r.outcome))
    out = {}
    for agent, pairs in history.items():
        recent = pairs[-window:]
        out[agent] = (sum((p - y) ** 2 for p, y in recent) / len(recent), len(recent))
    return out


def weight_for(brier: float, n: int) -> float:
    if n < MIN_RESOLVED:
        return 1.0
    w = math.exp(-K * (brier - REFERENCE_BRIER))
    return round(min(MAX_WEIGHT, max(MIN_WEIGHT, w)), 4)


def agent_weights(
    forecasts: Iterable[Forecast], resolutions: dict[str, ForecastResolution]
) -> dict[str, float]:
    return {a: weight_for(b, n) for a, (b, n) in agent_brier_scores(forecasts, resolutions).items()}
