"""Make forecasts for one asset at time T, with the AI panel or as a labelled baseline.

* With a provider: the panel's final probability (see :mod:`proofcast.agents.panel`), setup
  ``ai:<provider>:<model>``.
* With ``--no-ai``: the momentum baseline (base rate adjusted by recent momentum), setup
  ``baseline:momentum``, ``is_baseline = True``, clearly labelled everywhere.

Every forecast also stores the three baselines for the same question, so the track record
compares like with like. Only live forecasts may be written to the ledger
(:func:`assert_live`): backtests and forecasts with a past ``as_of`` never count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from proofcast import DISCLAIMER
from proofcast.agents.context import AgentContext, build_context
from proofcast.agents.panel import Panel, PanelResult
from proofcast.config import Config
from proofcast.forecast.baselines import make_rule, question_text
from proofcast.llm.base import Provider, TokenBudget
from proofcast.models import Forecast, SourceRef, stable_id
from proofcast.storage import DataStore

LIVE_TOLERANCE = timedelta(hours=1)
BACKTEST_WARNING = (
    "Backtest warning: this forecast uses a past date. Language models may have memorised what "
    "happened after it, so backtested AI accuracy is not real accuracy. It is never written to "
    "the public ledger."
)


class NotLiveError(RuntimeError):
    """Refused to record a forecast whose time is not now."""


def assert_live(as_of: datetime, now: datetime) -> None:
    if abs(now - as_of) > LIVE_TOLERANCE:
        raise NotLiveError(
            f"only live forecasts go into the ledger (as_of {as_of.isoformat()}, "
            f"now {now.isoformat()})"
        )


@dataclass
class AssetForecasts:
    context: AgentContext
    forecasts: list[Forecast]
    panel: PanelResult | None = None
    notes: list[str] = field(default_factory=list)


def make_forecasts(
    cfg: Config,
    store: DataStore,
    symbol: str,
    as_of: datetime,
    provider: Provider | None,
    *,
    budget: TokenBudget | None = None,
    weights: dict[str, float] | None = None,
) -> AssetForecasts:
    ctx = build_context(cfg, store, symbol, as_of)
    lo, hi = cfg.forecast.min_probability, cfg.forecast.max_probability
    ref = ctx.reference
    price_ref = SourceRef(
        id=f"P:{ref.asset}:{ref.date.isoformat()}",
        kind="price",
        title=f"{ref.asset} close {ref.close:g} on {ref.date} ({ref.source})",
        published_at=ref.available_at,
    )
    panel_result: PanelResult | None = None
    if provider is not None:
        panel_result = Panel(provider, budget=budget, weights=weights, lo=lo, hi=hi).run(ctx)
        setup = f"ai:{provider.name}:{provider.model}"
    else:
        setup = "baseline:momentum"
    items = ctx.by_id()
    forecasts: list[Forecast] = []
    for h in ctx.horizons:
        b = ctx.baselines[h]
        rule = make_rule(ref, h)
        if panel_result is None:
            probability = min(hi, max(lo, b.momentum))
            reasoning = (
                f"Baseline, no AI: historical base rate {b.base_rate:.0%} adjusted by recent "
                f"momentum to {probability:.0%}."
            )
            opinions = []
            cited: list[str] = []
            notes = []
            model = None
        else:
            hr = panel_result.horizons[h]
            probability = hr.probability
            reasoning = hr.reasoning or "The forecaster gave no reasoning."
            opinions = panel_result.opinions
            cited = sorted(set(hr.citations) | {c for o in opinions for c in o.citations})
            notes = hr.notes + panel_result.notes
            model = provider.model if provider else None
        sources = [price_ref] + [items[c].ref() for c in cited if c in items]
        forecasts.append(
            Forecast(
                id=stable_id("forecast", ref.asset, str(h), setup, as_of.isoformat()),
                created_at=as_of,
                asset=ref.asset,
                question=question_text(rule),
                horizon_days=h,
                probability=probability,
                base_rate=b.base_rate,
                baselines=b.as_dict(),
                resolution_rule=rule,
                setup=setup,
                is_baseline=panel_result is None,
                agent_opinions=opinions,
                final_reasoning=reasoning,
                sources=sources,
                model=model,
                untrusted_input_notes=notes[:50],
            )
        )
    return AssetForecasts(ctx, forecasts, panel_result)


def already_forecast(existing: list[Forecast], new: Forecast) -> bool:
    """One forecast per asset, horizon, setup and reference bar: re-runs add nothing."""
    key = (new.asset, new.horizon_days, new.setup, new.resolution_rule.reference_date)
    return any(
        (f.asset, f.horizon_days, f.setup, f.resolution_rule.reference_date) == key
        for f in existing
    )


def report_text(result: AssetForecasts, *, now: datetime | None = None) -> str:
    ctx = result.context
    now = now or datetime.now(UTC)
    lines = [
        f"Proofcast analysis: {ctx.asset.symbol} ({ctx.asset.name}) "
        f"at {ctx.as_of:%Y-%m-%d %H:%M} UTC",
        f"Reference close {ctx.reference.close:g} on {ctx.reference.date} ({ctx.reference.source})",
        "",
    ]
    if abs(now - ctx.as_of) > LIVE_TOLERANCE:
        lines += [BACKTEST_WARNING, ""]
    for f in result.forecasts:
        label = "BASELINE (no AI)" if f.is_baseline else f.setup
        lines.append(
            f"{f.horizon_days:>3}d  {f.probability:5.1%} chance of closing higher   "
            f"base rate {f.baselines['base_rate']:.1%}  momentum {f.baselines['momentum']:.1%}  "
            f"[{label}]"
        )
        lines.append(f"      {f.final_reasoning}")
    if result.panel is not None:
        lines += ["", "Panel:"]
        for o in result.panel.opinions:
            probs = ", ".join(f"{h}d {p:.0%}" for h, p in sorted(o.probabilities.items()))
            lines.append(
                f"  {o.agent:<10} conf {o.confidence:.2f} weight {o.weight:.2f}  {probs}  "
                f"{o.summary[:160]}"
            )
        notes = [n for f in result.forecasts for n in f.untrusted_input_notes]
        if notes:
            lines += ["", "Notes:"] + [f"  - {n}" for n in dict.fromkeys(notes)]
    srcs = {s.id: s for f in result.forecasts for s in f.sources}
    if srcs:
        lines += ["", "Sources:"]
        for s in srcs.values():
            url = f" <{s.url}>" if s.url else ""
            lines.append(f"  [{s.id}] {s.published_at:%Y-%m-%d} {s.title[:120]}{url}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
