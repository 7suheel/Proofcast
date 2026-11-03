"""Explanations for drawdowns: the History agent (AI) and a rules-based fallback (no AI).

The agent only sees the linked evidence, wrapped as untrusted data. Our code then:

* drops any citation that is not one of the evidence ids,
* drops contributors left without a valid citation,
* forces ``evidence_quality = "thin"`` when there are fewer than 3 evidence items,
* never calls the model when there is no evidence at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from proofcast.history.linker import DrawdownEvidence
from proofcast.llm.base import BudgetExceededError, Provider, TokenBudget
from proofcast.llm.jsonout import complete_json
from proofcast.models import Event
from proofcast.prompts import load_prompt
from proofcast.security import UNTRUSTED_RULES, wrap_untrusted

Quality = Literal["thin", "moderate", "strong"]

STANDARD_CAVEATS = [
    "Headlines show what was reported around the fall, not what caused it; timing is not proof "
    "of cause.",
    "Our news archive is incomplete, especially before 2017, so important stories may be missing.",
]


class _Contributor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(max_length=600)
    citations: list[str] = Field(default_factory=list, max_length=10)


class HistoryReply(BaseModel):
    """The JSON shape the History agent must return."""

    model_config = ConfigDict(extra="ignore")
    contributors: list[_Contributor] = Field(default_factory=list, max_length=8)
    cannot_tell: list[str] = Field(default_factory=list, max_length=8)
    evidence_quality: Quality = "thin"


@dataclass(frozen=True)
class Contributor:
    text: str
    citations: list[str]
    """Event ids (``news:...``, ``filing:...``, ``macro:...``)."""


@dataclass
class Explanation:
    contributors: list[Contributor]
    cannot_tell: list[str]
    evidence_quality: Quality
    generated_by: str
    """``ai:<provider>:<model>`` or ``rules``."""
    notes: list[str] = field(default_factory=list)


def _label_map(events: list[Event]) -> dict[str, Event]:
    return {f"E{i}": e for i, e in enumerate(events, start=1)}


def rules_explanation(evidence: DrawdownEvidence) -> Explanation:
    """A deterministic summary of the top evidence, used with ``--no-ai`` or as a fallback."""
    contributors: list[Contributor] = []
    ctx = evidence.context
    if ctx is not None and ctx.label in {"market-wide", "partly market-wide"}:
        contributors.append(
            Contributor(text=f"The whole market fell too. {ctx.sentence()}", citations=[])
        )
    # Company news and filings first; macro releases only fill remaining slots.
    ranked = [e for e in evidence.events if e.kind != "macro"]
    ranked += [e for e in evidence.events if e.kind == "macro"]
    for e in ranked[:3]:
        when = e.published_at.strftime("%Y-%m-%d")
        contributors.append(
            Contributor(text=f"Coincided with: {e.title} ({e.source}, {when})", citations=[e.id])
        )
    cannot = list(STANDARD_CAVEATS)
    if evidence.is_thin:
        cannot.insert(0, f"Evidence is thin: only {len(evidence.events)} linked item(s).")
    quality: Quality = "thin" if evidence.is_thin else "moderate"
    return Explanation(contributors, cannot, quality, generated_by="rules")


def explain_drawdown(
    evidence: DrawdownEvidence,
    provider: Provider | None,
    *,
    asset_name: str = "",
    budget: TokenBudget | None = None,
) -> Explanation:
    """Explain a drawdown with the History agent, or with rules when AI is off."""
    if provider is None:
        return rules_explanation(evidence)
    if not evidence.events:
        fallback = rules_explanation(evidence)
        fallback.notes.append("no evidence linked: the History agent was not called")
        return fallback

    labels = _label_map(evidence.events)
    wrapped = []
    flags: list[str] = []
    for label, e in labels.items():
        w = wrap_untrusted(label, e.kind, e.title, published_at=e.published_at, origin=e.source)
        wrapped.append(w.text)
        if w.flags:
            flags.append(f"{e.id}: possible prompt injection ({', '.join(w.flags)})")
    d = evidence.drawdown
    template = load_prompt("history")
    system, prompt = template.render(
        untrusted_rules=UNTRUSTED_RULES,
        asset=d.asset,
        asset_name=asset_name or d.asset,
        start=d.start.isoformat(),
        trough=d.trough.isoformat(),
        depth=f"{d.depth_pct:.1f}",
        recovery=d.recovery.isoformat() if d.recovery else "not recovered yet",
        market_context=evidence.context.sentence() if evidence.context else "unknown",
        count=str(len(labels)),
        evidence="\n".join(wrapped),
    )
    try:
        result = complete_json(provider, system, prompt, HistoryReply, budget=budget)
    except BudgetExceededError as exc:
        fallback = rules_explanation(evidence)
        fallback.notes.append(str(exc))
        return fallback
    if result.value is None:
        fallback = rules_explanation(evidence)
        fallback.notes.extend(result.notes)
        fallback.notes.extend(flags)
        return fallback

    reply = result.value
    notes = list(result.notes) + flags
    contributors: list[Contributor] = []
    for c in reply.contributors[:4]:
        valid = [labels[x.strip("[] ")].id for x in c.citations if x.strip("[] ") in labels]
        dropped = [x for x in c.citations if x.strip("[] ") not in labels]
        if dropped:
            notes.append(f"dropped unknown citations {dropped}")
        if not valid:
            notes.append("dropped a contributor without valid citations")
            continue
        contributors.append(Contributor(text=c.text.strip(), citations=valid))
    quality: Quality = reply.evidence_quality
    cannot = [t.strip() for t in reply.cannot_tell[:4] if t.strip()]
    if evidence.is_thin or not contributors:
        quality = "thin"
        thin_note = f"Evidence is thin: only {len(evidence.events)} linked item(s)."
        if thin_note not in cannot:
            cannot.insert(0, thin_note)
    for caveat in STANDARD_CAVEATS[:1]:
        if caveat not in cannot:
            cannot.append(caveat)
    return Explanation(
        contributors=contributors,
        cannot_tell=cannot,
        evidence_quality=quality,
        generated_by=f"ai:{provider.name}:{provider.model}",
        notes=notes,
    )
