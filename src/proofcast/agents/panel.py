"""Run the analyst panel and the forecaster, then turn their output into final probabilities.

Order: News, Filings, Technical, Macro and History analysts (each sees only its part of the
context) -> Bull and Bear (see everything plus the analysts' notes) -> Skeptic -> Forecaster.
An analyst whose part of the context is empty is skipped instead of being asked to guess.

Our code, not the models, has the last word:

* **Citations.** Ids not in the context are dropped. An agent's confidence is multiplied by
  ``1 - 0.5 * dropped / cited``, and halved again if no valid citation is left.
* **Panel average.** Weighted mean of the agents' probabilities with weight
  ``track_record_weight * confidence``; *disagreement* is the weighted standard deviation.
* **Shrinkage.** ``p = base_rate + s * (p_forecaster - base_rate)`` with ``s = 1``, halved when
  fewer than :data:`MIN_VALID_SOURCES` distinct context items were validly cited (thin
  evidence), and halved again when disagreement exceeds :data:`MAX_DISAGREEMENT`.
* **Clamp** to ``[min_probability, max_probability]`` (default 0.05 to 0.95).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from proofcast.agents.context import AgentContext
from proofcast.forecast.baselines import clamp
from proofcast.llm.base import BudgetExceededError, Provider, TokenBudget
from proofcast.llm.jsonout import complete_json
from proofcast.models import AgentOpinion, Probability
from proofcast.prompts import load_prompt
from proofcast.security import UNTRUSTED_RULES

MIN_VALID_SOURCES = 3
MAX_DISAGREEMENT = 0.15
AGENT_MAX_TOKENS = 1500

ANALYSTS: dict[str, tuple[str, frozenset[str]]] = {
    "news": ("news", frozenset({"news", "base_rate"})),
    "filings": ("filings", frozenset({"filing", "fact", "base_rate"})),
    "technical": ("technical", frozenset({"indicator", "base_rate"})),
    "macro": ("macro", frozenset({"macro", "base_rate"})),
    "history": ("history_panel", frozenset({"history", "indicator", "base_rate"})),
}
EVIDENCE_KINDS = {
    "news": {"news"},
    "filings": {"filing", "fact"},
    "technical": {"indicator"},
    "macro": {"macro"},
    "history": {"history"},
}
DEBATERS = ("bull", "bear", "skeptic")
ALL_KINDS = frozenset({"news", "filing", "fact", "macro", "indicator", "base_rate", "history"})


class AgentReply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    probabilities: dict[str, Probability]
    confidence: Probability = 0.5
    summary: str = Field(default="", max_length=1500)
    citations: list[str] = Field(default_factory=list, max_length=40)


class _HorizonForecast(BaseModel):
    model_config = ConfigDict(extra="ignore")
    horizon_days: int
    probability: Probability
    reasoning: str = Field(default="", max_length=1500)
    citations: list[str] = Field(default_factory=list, max_length=40)


class ForecasterReply(BaseModel):
    model_config = ConfigDict(extra="ignore")
    forecasts: list[_HorizonForecast] = Field(max_length=10)


@dataclass
class HorizonResult:
    horizon_days: int
    probability: float
    base_rate: float
    model_probability: float | None
    panel_average: float | None
    disagreement: float
    reasoning: str
    citations: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass
class PanelResult:
    opinions: list[AgentOpinion]
    horizons: dict[int, HorizonResult]
    notes: list[str]
    valid_sources: list[str]


def filter_citations(cited: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    clean = [c.strip().strip("[]") for c in cited]
    valid = list(dict.fromkeys(c for c in clean if c in allowed))
    dropped = [c for c in clean if c not in allowed]
    return valid, dropped


def adjusted_confidence(confidence: float, n_cited: int, n_dropped: int, n_valid: int) -> float:
    if n_cited:
        confidence *= 1 - 0.5 * (n_dropped / n_cited)
    if n_valid == 0:
        confidence *= 0.5
    return round(confidence, 4)


def weighted_stats(opinions: list[AgentOpinion], horizon: int) -> tuple[float | None, float]:
    pairs = [
        (o.probabilities[horizon], o.weight * o.confidence)
        for o in opinions
        if horizon in o.probabilities
    ]
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return None, 0.0
    avg = sum(p * w for p, w in pairs) / total
    var = sum(w * (p - avg) ** 2 for p, w in pairs) / total
    return avg, math.sqrt(var)


def finalize(
    horizon: int,
    base: float,
    model_p: float | None,
    panel_avg: float | None,
    disagreement: float,
    n_valid_sources: int,
    *,
    lo: float = 0.05,
    hi: float = 0.95,
) -> tuple[float, list[str]]:
    """Shrink toward the base rate when evidence is thin or the panel disagrees; clamp."""
    notes: list[str] = []
    target = model_p
    if target is None:
        target = panel_avg if panel_avg is not None else base
        notes.append(
            "forecaster unavailable: used the panel average"
            if panel_avg is not None
            else "no usable model output: used the base rate"
        )
    s = 1.0
    if n_valid_sources < MIN_VALID_SOURCES:
        s *= 0.5
        notes.append(f"thin evidence ({n_valid_sources} valid sources): pulled toward base rate")
    if disagreement > MAX_DISAGREEMENT:
        s *= 0.5
        notes.append(
            f"agents disagree strongly (spread {disagreement:.2f}): pulled toward base rate"
        )
    p = base + s * (target - base)
    final = clamp(p, lo, hi)
    if final != p:
        notes.append(f"clamped from {p:.3f} to [{lo}, {hi}]")
    return round(final, 4), notes


class Panel:
    def __init__(
        self,
        provider: Provider,
        *,
        budget: TokenBudget | None = None,
        weights: dict[str, float] | None = None,
        lo: float = 0.05,
        hi: float = 0.95,
    ) -> None:
        self.provider = provider
        self.budget = budget or TokenBudget()
        self.weights = weights or {}
        self.lo, self.hi = lo, hi

    # -- prompt helpers ------------------------------------------------------------

    def _shared(self, ctx: AgentContext) -> tuple[str, str]:
        keys = "{" + ", ".join(f'"{h}": 0.0' for h in ctx.horizons) + "}"
        return load_prompt("panel_rules").render(untrusted_rules=UNTRUSTED_RULES, horizon_keys=keys)

    def _ask_agent(
        self, name: str, prompt_name: str, ctx: AgentContext, kinds: frozenset[str], notes: str
    ) -> tuple[AgentOpinion | None, list[str]]:
        rules, reply_format = self._shared(ctx)
        template = load_prompt(prompt_name)
        values = {
            "rules": rules,
            "reply_format": reply_format,
            "header": ctx.header(),
            "context": ctx.render(kinds),
            "notes": notes,
        }
        system, prompt = template.render(
            **{k: v for k, v in values.items() if k in template.placeholders()}
        )
        result = complete_json(
            self.provider,
            system,
            prompt,
            AgentReply,
            budget=self.budget,
            max_tokens=AGENT_MAX_TOKENS,
        )
        out_notes = [f"{name}: {n}" for n in result.notes]
        if result.value is None:
            return None, out_notes
        reply = result.value
        valid, dropped = filter_citations(reply.citations, ctx.ids())
        if dropped:
            out_notes.append(f"{name}: dropped citations not in context: {dropped[:10]}")
        probs: dict[int, float] = {}
        for key, p in reply.probabilities.items():
            try:
                h = int(str(key).strip())
            except ValueError:
                continue
            if h in ctx.horizons:
                probs[h] = float(p)
        return (
            AgentOpinion(
                agent=name,
                probabilities=probs,
                confidence=adjusted_confidence(
                    reply.confidence, len(reply.citations), len(dropped), len(valid)
                ),
                summary=reply.summary.strip(),
                citations=valid,
                dropped_citations=dropped[:20],
                weight=self.weights.get(name, 1.0),
            ),
            out_notes,
        )

    @staticmethod
    def _notes_text(opinions: list[AgentOpinion]) -> str:
        if not opinions:
            return "(none)"
        rows = []
        for o in opinions:
            probs = ", ".join(f"{h}d: {p:.2f}" for h, p in sorted(o.probabilities.items()))
            rows.append(
                f"- {o.agent} (weight {o.weight:.2f}, confidence {o.confidence:.2f}; {probs}): "
                f"{o.summary} [cites: {', '.join(o.citations) or 'none'}]"
            )
        return "\n".join(rows)

    # -- run -----------------------------------------------------------------------

    def run(self, ctx: AgentContext) -> PanelResult:
        opinions: list[AgentOpinion] = []
        notes: list[str] = list(ctx.injection_notes)
        try:
            for name, (prompt_name, kinds) in ANALYSTS.items():
                if not ctx.of_kinds(EVIDENCE_KINDS[name]):
                    notes.append(
                        f"{name}: no {'/'.join(sorted(EVIDENCE_KINDS[name]))} items, skipped"
                    )
                    continue
                op, n = self._ask_agent(name, prompt_name, ctx, kinds, "")
                notes += n
                if op is not None:
                    opinions.append(op)
            analyst_notes = self._notes_text(opinions)
            for name in DEBATERS:
                notes_text = self._notes_text(opinions) if name == "skeptic" else analyst_notes
                op, n = self._ask_agent(name, name, ctx, ALL_KINDS, notes_text)
                notes += n
                if op is not None:
                    opinions.append(op)
            forecaster = self._ask_forecaster(ctx, opinions, notes)
        except BudgetExceededError as exc:
            notes.append(f"stopped early: {exc}")
            forecaster = {}
        return self._combine(ctx, opinions, forecaster, notes)

    def _ask_forecaster(
        self, ctx: AgentContext, opinions: list[AgentOpinion], notes: list[str]
    ) -> dict[int, _HorizonForecast]:
        template = load_prompt("forecaster")
        averages = {h: weighted_stats(opinions, h)[0] for h in ctx.horizons}
        panel_avg = json.dumps(
            {str(h): None if a is None else round(a, 3) for h, a in averages.items()}
        )
        system, prompt = template.render(
            untrusted_rules=UNTRUSTED_RULES,
            header=ctx.header(),
            context=ctx.render(ALL_KINDS),
            notes=self._notes_text(opinions),
            panel_average=panel_avg,
            horizons=", ".join(str(h) for h in ctx.horizons),
        )
        result = complete_json(
            self.provider,
            system,
            prompt,
            ForecasterReply,
            budget=self.budget,
            max_tokens=AGENT_MAX_TOKENS,
        )
        notes += [f"forecaster: {n}" for n in result.notes]
        if result.value is None:
            return {}
        return {f.horizon_days: f for f in result.value.forecasts if f.horizon_days in ctx.horizons}

    def _combine(
        self,
        ctx: AgentContext,
        opinions: list[AgentOpinion],
        forecaster: dict[int, _HorizonForecast],
        notes: list[str],
    ) -> PanelResult:
        allowed = ctx.ids()
        valid_sources: set[str] = set()
        for o in opinions:
            valid_sources.update(c for c in o.citations if not c.startswith("B:"))
        horizons: dict[int, HorizonResult] = {}
        for h in ctx.horizons:
            base = ctx.baselines[h].base_rate
            avg, spread = weighted_stats(opinions, h)
            f = forecaster.get(h)
            cites: list[str] = []
            reasoning = ""
            if f is not None:
                cites, dropped = filter_citations(f.citations, allowed)
                if dropped:
                    notes.append(
                        f"forecaster {h}d: dropped citations not in context: {dropped[:10]}"
                    )
                reasoning = f.reasoning.strip()
            sources_here = valid_sources | {c for c in cites if not c.startswith("B:")}
            p, fin_notes = finalize(
                h,
                base,
                f.probability if f else None,
                avg,
                spread,
                len(sources_here),
                lo=self.lo,
                hi=self.hi,
            )
            horizons[h] = HorizonResult(
                horizon_days=h,
                probability=p,
                base_rate=base,
                model_probability=None if f is None else round(f.probability, 4),
                panel_average=None if avg is None else round(avg, 4),
                disagreement=round(spread, 4),
                reasoning=reasoning,
                citations=cites,
                notes=fin_notes,
            )
            valid_sources |= set(cites)
        return PanelResult(opinions, horizons, notes, sorted(valid_sources))
