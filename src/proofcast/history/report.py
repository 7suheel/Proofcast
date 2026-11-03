"""Plain-text reports for ``proofcast history`` and ``proofcast why``."""

from __future__ import annotations

from datetime import date

from proofcast import DISCLAIMER
from proofcast.config import Config
from proofcast.history.drawdowns import default_threshold, detect_drawdowns, find_drawdown
from proofcast.history.explain import Explanation, explain_drawdown
from proofcast.history.linker import DrawdownEvidence, link_evidence
from proofcast.llm.base import Provider, TokenBudget
from proofcast.models import Drawdown
from proofcast.storage import DataStore


def asset_drawdowns(cfg: Config, store: DataStore, symbol: str) -> list[Drawdown]:
    asset = cfg.asset(symbol)
    threshold = default_threshold(
        asset.kind, cfg.history.stock_drawdown_threshold, cfg.history.crypto_drawdown_threshold
    )
    bars = store.read_bars(asset.symbol)
    if not bars:
        return []
    since = date(bars[-1].date.year - cfg.history.years, bars[-1].date.month, 1)
    return detect_drawdowns(bars, threshold, since=since)


def history_text(cfg: Config, store: DataStore, symbol: str) -> str:
    asset = cfg.asset(symbol)
    drawdowns = asset_drawdowns(cfg, store, asset.symbol)
    if not store.read_bars(asset.symbol):
        return f"No price data for {asset.symbol}. Run `proofcast fetch prices -a {asset.symbol}`."
    lines = [f"Major drawdowns for {asset.symbol} ({asset.name}), last {cfg.history.years} years:"]
    if not drawdowns:
        lines.append("  none deeper than the threshold")
    for d in drawdowns:
        recovery = d.recovery.isoformat() if d.recovery else "not recovered"
        lines.append(
            f"  {d.start}  ->  trough {d.trough}  {-d.depth_pct:6.1f}%   recovery: {recovery}"
        )
    lines += ["", f"Use `proofcast why {asset.symbol} <date>` for evidence.", DISCLAIMER]
    return "\n".join(lines)


def why(
    cfg: Config,
    store: DataStore,
    symbol: str,
    day: date,
    provider: Provider | None,
    budget: TokenBudget | None = None,
) -> tuple[DrawdownEvidence, Explanation] | None:
    asset = cfg.asset(symbol)
    drawdown = find_drawdown(asset_drawdowns(cfg, store, asset.symbol), day)
    if drawdown is None:
        return None
    evidence = link_evidence(drawdown, store, cfg)
    explanation = explain_drawdown(evidence, provider, asset_name=asset.name, budget=budget)
    return evidence, explanation


def why_text(evidence: DrawdownEvidence, explanation: Explanation) -> str:
    d = evidence.drawdown
    recovery = d.recovery.isoformat() if d.recovery else "not recovered yet"
    lines = [
        f"Why did {d.asset} fall {d.depth_pct:.1f}% from {d.start} to {d.trough}?"
        f" (recovery: {recovery})",
        "",
    ]
    if evidence.context is not None:
        lines += ["Market context: " + evidence.context.sentence(), ""]
    by_id = {e.id: e for e in evidence.events}
    lines.append(f"Most likely contributors ({explanation.generated_by}):")
    if not explanation.contributors:
        lines.append("  - none we can support with the linked evidence")
    for c in explanation.contributors:
        refs = ", ".join(
            f"{by_id[x].source} {by_id[x].published_at:%Y-%m-%d}" for x in c.citations if x in by_id
        )
        lines.append(f"  - {c.text}" + (f"  [{refs}]" if refs else ""))
    lines += ["", "What we can't tell:"]
    lines += [f"  - {t}" for t in explanation.cannot_tell]
    lines += ["", f"Evidence quality: {explanation.evidence_quality}", "", "Linked evidence:"]
    if not evidence.events:
        lines.append("  (none found in the stored news, filings and macro releases)")
    for e in evidence.events:
        lines.append(
            f"  {e.published_at:%Y-%m-%d}  {e.kind:6}  rel {e.relevance:.2f}  {e.title}"
            + (f"  <{e.url}>" if e.url else "")
        )
    if explanation.notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in explanation.notes]
    lines += ["", DISCLAIMER]
    return "\n".join(lines)
