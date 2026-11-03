"""Map free text (headlines, summaries) to assets in the universe.

Matching rules, chosen to avoid false positives:

* company names and aliases of 5+ characters match case-insensitively on word boundaries;
  shorter ones (``Meta``, ``Coke``, ``SOL``) only match as written, capitalised or upper-case,
  so common words such as "meta-analysis" do not count;
* tickers of 3+ characters match case-sensitively as whole words (``AAPL``);
* short tickers (``KO``) only match in explicit ticker forms: ``$KO``, ``(KO)``, ``NYSE: KO``.

The patterns are built from escaped literals joined with ``|`` and only bounded look-arounds,
so matching is linear in the text length (no catastrophic backtracking). Input is truncated
to :data:`MAX_TEXT_CHARS` first.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from proofcast.models import Asset

MAX_TEXT_CHARS = 20_000
_EXCHANGES = r"(?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX)"


def normalize_text(text: str) -> str:
    """NFKC-normalise and drop invisible format characters (zero-width joiners, bidi marks).

    This defeats look-alike tricks such as a zero-width space inside a ticker or full-width
    letters (U+FF21 and friends) that render like ``AAPL``.
    """
    text = unicodedata.normalize("NFKC", text[:MAX_TEXT_CHARS])
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


@dataclass(frozen=True)
class _AssetPatterns:
    symbol: str
    names: re.Pattern[str] | None
    ticker: re.Pattern[str]


class AssetMatcher:
    """Find which assets a text mentions."""

    def __init__(self, assets: Iterable[Asset]) -> None:
        self._patterns: list[_AssetPatterns] = []
        for a in assets:
            names = {a.name, *a.aliases}
            names |= {strip_company_suffix(n) for n in names}
            words = {n.strip() for n in names if len(n.strip()) >= 3}
            long_words = sorted((w for w in words if len(w) >= 5), key=len, reverse=True)
            short_words = sorted(
                {v for w in words if len(w) < 5 for v in _case_variants(w)},
                key=len,
                reverse=True,
            )
            alternatives = [f"(?i:{re.escape(w)})" for w in long_words]
            alternatives += [re.escape(w) for w in short_words]
            name_re = (
                re.compile(r"(?<![\w])(?:" + "|".join(alternatives) + r")(?![\w])")
                if alternatives
                else None
            )
            sym = re.escape(a.symbol)
            explicit = rf"\${sym}\b|\({sym}\)|{_EXCHANGES}:\s?{sym}\b"
            ticker_re = (
                re.compile(rf"(?<![\w$]){sym}(?![\w])|{explicit}")
                if len(a.symbol) >= 3
                else re.compile(explicit)
            )
            self._patterns.append(_AssetPatterns(a.symbol, name_re, ticker_re))

    def match(self, *texts: str | None) -> list[str]:
        """Symbols mentioned in any of ``texts``, sorted."""
        joined = normalize_text(" \n ".join(t for t in texts if t))
        found = set()
        for p in self._patterns:
            if p.ticker.search(joined) or (p.names is not None and p.names.search(joined)):
                found.add(p.symbol)
        return sorted(found)


_SUFFIXES = re.compile(
    r",?\s+(?:Inc\.?|Corporation|Corp\.?|Co\.?|Company|& Co\.?|Platforms|N\.V\.|plc)$",
    re.IGNORECASE,
)


def strip_company_suffix(name: str) -> str:
    """``The Coca-Cola Company`` -> ``Coca-Cola``; ``Apple Inc.`` -> ``Apple``."""
    name = name.strip()
    if name.lower().startswith("the "):
        name = name[4:]
    previous = None
    while previous != name:
        previous = name
        name = _SUFFIXES.sub("", name).strip()
    return name


def _case_variants(word: str) -> set[str]:
    """``meta`` -> {``Meta``, ``META``}; a mixed-case alias also matches as written."""
    variants = {word.capitalize(), word.upper()}
    if word != word.lower():
        variants.add(word)
    return variants
