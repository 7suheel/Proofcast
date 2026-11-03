"""News adapters: RSS/Atom feeds and the GDELT DOC 2.0 API.

News text is untrusted (hard rule 6). Here we only parse and store it: HTML is stripped,
lengths are capped, and the text is never interpreted. Prompt wrapping happens in
:mod:`proofcast.agents`.

Publication time:

* RSS/Atom: the item's ``published``/``updated`` date. Items without a date get the fetch time,
  which is the earliest moment we can prove the item was public. Dates in the future (clock
  skew, bad feeds) are clamped to the fetch time.
* GDELT: ``seendate``, the time GDELT first saw the article.
"""

from __future__ import annotations

import calendar
import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import feedparser

from proofcast.adapters.base import Clock, utcnow
from proofcast.adapters.mapping import AssetMatcher, normalize_text, strip_company_suffix
from proofcast.config import RssFeed
from proofcast.models import Asset, NewsItem
from proofcast.net.http import HttpClient, SourceBlockedError

MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 1_000
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS_PER_FEED = 500
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_HOST = "api.gdeltproject.org"

_TAG = re.compile(r"<[^<>]{0,2000}>")
_SPACE = re.compile(r"\s+")


def clean_text(raw: str | None, limit: int) -> str:
    """Strip tags and entities, collapse whitespace, cap length. Linear time."""
    if not raw:
        return ""
    text = raw[: limit * 20]
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = normalize_text(text)
    text = _SPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return url


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(calendar.timegm(value), UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def parse_feed(
    text: str,
    feed: RssFeed,
    matcher: AssetMatcher,
    fetched_at: datetime,
    *,
    keep_unmatched: bool = False,
) -> list[NewsItem]:
    """Parse RSS/Atom text into news items tagged with the assets they mention."""
    if len(text.encode("utf-8", errors="ignore")) > MAX_FEED_BYTES:
        raise SourceBlockedError(f"feed {feed.name} is larger than {MAX_FEED_BYTES} bytes")
    parsed = feedparser.parse(text, sanitize_html=True, resolve_relative_uris=False)
    items: list[NewsItem] = []
    for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
        url = _safe_url(entry.get("link"))
        title = clean_text(entry.get("title"), MAX_TITLE_CHARS)
        if not url or not title:
            continue
        summary = clean_text(entry.get("summary") or entry.get("description"), MAX_SUMMARY_CHARS)
        published = _entry_time(entry) or fetched_at
        if published > fetched_at:
            published = fetched_at
        assets = matcher.match(title, summary)
        if not assets and not keep_unmatched:
            continue
        items.append(
            NewsItem(
                id=NewsItem.make_id(url, title),
                title=title,
                url=url,
                source=feed.name,
                published_at=published,
                fetched_at=fetched_at,
                assets=assets,
                summary=summary or None,
            )
        )
    return items


def fetch_feed(
    client: HttpClient, feed: RssFeed, matcher: AssetMatcher, *, clock: Clock = utcnow
) -> list[NewsItem]:
    text = client.get_text(
        feed.url,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9"},
        cache_ttl=600,
    )
    return parse_feed(text, feed, matcher, clock())


# ---------------------------------------------------------------------------
# GDELT
# ---------------------------------------------------------------------------


MARKET_TERMS = {
    "stock": "(stock OR shares OR earnings OR investors OR analysts)",
    "crypto": "(price OR crypto OR market OR traders OR ETF)",
}


def gdelt_query(asset: Asset) -> str:
    """A GDELT query: the asset's main name in quotes, plus market words, English sources."""
    name = strip_company_suffix(asset.name)
    name = re.sub(r"[^\w .&-]", "", name)
    return f'"{name}" {MARKET_TERMS[asset.kind]} sourcelang:english'


def parse_gdelt_seendate(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def parse_gdelt(
    payload: Any, asset: Asset, matcher: AssetMatcher, fetched_at: datetime
) -> list[NewsItem]:
    if not isinstance(payload, dict):
        raise SourceBlockedError("gdelt: unexpected payload")
    articles = payload.get("articles") or []
    items = []
    for art in articles[:MAX_ITEMS_PER_FEED]:
        if not isinstance(art, dict):
            continue
        url = _safe_url(art.get("url"))
        title = clean_text(art.get("title"), MAX_TITLE_CHARS)
        seen = art.get("seendate")
        if not url or not title or not isinstance(seen, str):
            continue
        try:
            published = parse_gdelt_seendate(seen)
        except ValueError:
            continue
        published = min(published, fetched_at)
        assets = matcher.match(title)
        if asset.symbol not in assets:
            # GDELT matches article bodies; keep only articles whose headline names the asset.
            continue
        domain = clean_text(art.get("domain"), 100) or "gdelt"
        items.append(
            NewsItem(
                id=NewsItem.make_id(url, title),
                title=title,
                url=url,
                source=f"gdelt:{domain}",
                published_at=published,
                fetched_at=fetched_at,
                assets=assets,
                summary=None,
            )
        )
    return items


def fetch_gdelt(
    client: HttpClient,
    asset: Asset,
    matcher: AssetMatcher,
    start: datetime,
    end: datetime,
    *,
    min_interval: float = 5.5,
    max_records: int = 250,
    clock: Clock = utcnow,
) -> list[NewsItem]:
    """Articles about ``asset`` seen by GDELT between ``start`` and ``end`` (UTC).

    GDELT asks for at most one request every 5 seconds; the client's rate limiter enforces it.
    The DOC API covers roughly 2017 onwards.
    """
    client.rate_limiter.set_interval(GDELT_HOST, min_interval)
    now = clock()
    end = min(end, now)
    if end <= start:
        return []
    # Old windows do not change; recent ones do.
    ttl = timedelta(days=30) if end < now - timedelta(days=2) else timedelta(hours=1)
    payload = client.get_json(
        GDELT_URL,
        params={
            "query": gdelt_query(asset),
            "mode": "artlist",
            "format": "json",
            "maxrecords": min(max_records, 250),
            "sort": "hybridrel",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        },
        cache_ttl=ttl.total_seconds(),
    )
    return parse_gdelt(payload, asset, matcher, now)
