"""Build what the agents may see at forecast time T (hard rules 4 and 6).

Everything comes from :func:`proofcast.pit.get_data` (published strictly before T), and the
finished context is checked again with :func:`proofcast.pit.assert_no_lookahead`. Text from
news and filings is wrapped as ``<untrusted_source>``; numbers our own code computed
(indicators, base rates, drawdowns, macro values, SEC figures) are rendered as ``<data>``.

Context ids: ``N#`` news, ``F#`` filings, ``K#`` reported figures, ``M#`` macro, ``T:<name>``
technical indicators, ``B:<horizon>`` base rates, ``H#`` history.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from proofcast.config import Config
from proofcast.forecast.baselines import Baselines, compute_baselines
from proofcast.forecast.indicators import compute_indicators
from proofcast.history.drawdowns import default_threshold, detect_drawdowns
from proofcast.models import Asset, FinancialFact, PriceBar, SourceRef
from proofcast.pit import assert_no_lookahead, get_data
from proofcast.security import wrap_untrusted
from proofcast.storage import DataStore

Kind = Literal["news", "filing", "fact", "macro", "indicator", "base_rate", "history"]
UNTRUSTED_KINDS = frozenset({"news", "filing"})

NEWS_DAYS = 30
MAX_NEWS = 25
FILING_DAYS = 180
MAX_FILINGS = 10
FACTS_PER_CONCEPT = 2


class NoPriceDataError(RuntimeError):
    """No bar was complete before the forecast time."""


@dataclass(frozen=True)
class ContextItem:
    id: str
    kind: Kind
    text: str
    published_at: datetime
    source: str
    url: str | None = None

    @property
    def trusted(self) -> bool:
        return self.kind not in UNTRUSTED_KINDS

    def ref(self) -> SourceRef:
        kind_map: dict[str, Literal["news", "filing", "macro", "price", "indicator"]] = {
            "news": "news",
            "filing": "filing",
            "fact": "filing",
            "macro": "macro",
        }
        return SourceRef(
            id=self.id,
            kind=kind_map.get(self.kind, "indicator"),
            title=self.text[:200],
            url=self.url,
            published_at=self.published_at,
        )


@dataclass
class AgentContext:
    asset: Asset
    as_of: datetime
    reference: PriceBar
    horizons: list[int]
    baselines: dict[int, Baselines]
    items: list[ContextItem] = field(default_factory=list)
    injection_notes: list[str] = field(default_factory=list)

    def ids(self) -> set[str]:
        return {i.id for i in self.items}

    def by_id(self) -> dict[str, ContextItem]:
        return {i.id: i for i in self.items}

    def of_kinds(self, kinds: Iterable[str]) -> list[ContextItem]:
        wanted = set(kinds)
        return [i for i in self.items if i.kind in wanted]

    def render(self, kinds: Iterable[str]) -> str:
        """Render items for a prompt; untrusted text is wrapped and flagged."""
        lines = []
        for item in self.of_kinds(kinds):
            if item.trusted:
                lines.append(
                    f'<data id="{item.id}" kind="{item.kind}" as_of="'
                    f'{item.published_at.isoformat()}">{item.text}</data>'
                )
            else:
                lines.append(
                    wrap_untrusted(
                        item.id,
                        item.kind,
                        item.text,
                        published_at=item.published_at,
                        origin=item.source,
                    ).text
                )
        return "\n".join(lines) if lines else "(no items)"

    def header(self) -> str:
        ref = self.reference
        rates = ", ".join(
            f"{h} days: {b.base_rate:.0%} (from {b.base_rate_n} past windows)"
            for h, b in sorted(self.baselines.items())
        )
        return (
            f"Asset: {self.asset.symbol} ({self.asset.name}, {self.asset.kind})\n"
            f"Forecast time: {self.as_of.isoformat()}\n"
            f"Reference close: {ref.close:g} on {ref.date.isoformat()} (last complete bar)\n"
            f"Question per horizon: will the close of the first trading day on or after "
            f"reference date + horizon be higher than the reference close?\n"
            f"Horizons (days): {', '.join(str(h) for h in self.horizons)}\n"
            f"Historical base rates of 'higher': {rates}"
        )


def _fmt(value: float) -> str:
    return f"{value:,.4g}" if abs(value) < 1e6 else f"{value:,.0f}"


def build_context(cfg: Config, store: DataStore, symbol: str, as_of: datetime) -> AgentContext:
    asset = cfg.asset(symbol)
    snap = get_data(
        store,
        asset.symbol,
        as_of,
        macro_series=[m.series_id for m in cfg.macro],
        news_lookback_days=NEWS_DAYS,
    )
    ref = snap.latest_bar()
    if ref is None:
        raise NoPriceDataError(f"no {asset.symbol} price bar was complete before {as_of}")
    horizons = list(cfg.forecast.horizons)
    baselines = {
        h: compute_baselines(snap.bars, h, as_of, lookback_years=cfg.history.years)
        for h in horizons
    }
    ctx = AgentContext(asset, as_of, ref, horizons, baselines)
    items = ctx.items

    for i, n in enumerate(
        sorted(snap.news, key=lambda n: n.published_at, reverse=True)[:MAX_NEWS], 1
    ):
        text = n.title + (f" - {n.summary[:400]}" if n.summary else "")
        items.append(ContextItem(f"N{i}", "news", text, n.published_at, n.source, n.url))

    recent_filings = [f for f in snap.filings if f.filed_at >= as_of - timedelta(days=FILING_DAYS)]
    for i, f in enumerate(
        sorted(recent_filings, key=lambda f: f.filed_at, reverse=True)[:MAX_FILINGS], 1
    ):
        items_txt = f" items {', '.join(f.items)}" if f.items else ""
        text = f"{f.form_type}{items_txt}" + (f": {f.display_title}" if f.display_title else "")
        items.append(ContextItem(f"F{i}", "filing", text, f.filed_at, "SEC EDGAR", f.url))

    facts = [f for f in store.read_facts(asset.symbol) if f.published_at < as_of]
    # The latest FACTS_PER_CONCEPT periods of each reported figure.
    by_concept: dict[tuple[str, str], list[FinancialFact]] = {}
    for fact in sorted(facts, key=lambda f: f.period_end, reverse=True):
        bucket = by_concept.setdefault((fact.concept, fact.unit), [])
        if len(bucket) < FACTS_PER_CONCEPT:
            bucket.append(fact)
    latest = sorted(
        (f for bucket in by_concept.values() for f in bucket),
        key=lambda f: (f.concept, f.period_end),
    )
    for k, fact in enumerate(latest, 1):
        text = (
            f"{fact.concept} = {_fmt(fact.value)} {fact.unit} for the period ending "
            f"{fact.period_end} ({fact.form}, filed {fact.filed})"
        )
        items.append(ContextItem(f"K{k}", "fact", text, fact.published_at, "SEC XBRL"))

    m = 0
    names = {s.series_id: s.name for s in cfg.macro}
    for series_id, obs in snap.macro.items():
        if not obs:
            continue
        m += 1
        last = obs[-1]
        prev = f"; previous {obs[-2].value:g}" if len(obs) > 1 else ""
        est = " (release time estimated)" if last.published_at_estimated else ""
        text = f"{names.get(series_id, series_id)}: {last.value:g} for {last.date}{prev}{est}"
        items.append(ContextItem(f"M{m}", "macro", text, last.published_at, "FRED"))

    for ind in compute_indicators(snap.bars, asset.kind):
        items.append(
            ContextItem(
                ind.id.replace("I:", "T:"), "indicator", ind.text(), ref.available_at, "Proofcast"
            )
        )
    for h, b in sorted(baselines.items()):
        text = (
            f"Share of past {h}-day windows that ended higher: {b.base_rate:.1%} "
            f"(n={b.base_rate_n}); momentum baseline {b.momentum:.1%}"
        )
        items.append(ContextItem(f"B:{h}", "base_rate", text, ref.available_at, "Proofcast"))

    threshold = default_threshold(
        asset.kind, cfg.history.stock_drawdown_threshold, cfg.history.crypto_drawdown_threshold
    )
    drawdowns = detect_drawdowns(snap.bars, threshold)
    for j, d in enumerate(drawdowns[-3:], 1):
        rec = f"recovered {d.recovery}" if d.recovery else "not recovered"
        text = f"Past drawdown: -{d.depth_pct:.1f}% from {d.start} to {d.trough}, {rec}"
        items.append(ContextItem(f"H{j}", "history", text, ref.available_at, "Proofcast"))
    peak = max(b.close for b in snap.bars)
    items.append(
        ContextItem(
            "H0",
            "history",
            f"Current close is {(ref.close / peak - 1) * 100:+.1f}% from the highest close "
            "on record",
            ref.available_at,
            "Proofcast",
        )
    )

    for item in items:
        if item.kind in UNTRUSTED_KINDS:
            w = wrap_untrusted(item.id, item.kind, item.text)
            if w.flags:
                ctx.injection_notes.append(
                    f"{item.id} ({item.source}): possible prompt injection ({', '.join(w.flags)})"
                )
    assert_no_lookahead([i.ref() for i in items], as_of)
    assert_no_lookahead(snap.bars, as_of)
    return ctx
