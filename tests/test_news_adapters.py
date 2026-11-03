from datetime import UTC, datetime

import pytest

from conftest import REPO_ROOT
from mock_http import Router, fixture_text
from proofcast.adapters import news
from proofcast.adapters.mapping import AssetMatcher, normalize_text, strip_company_suffix
from proofcast.config import RssFeed, load_config
from proofcast.net.http import SourceBlockedError

CFG = load_config(REPO_ROOT / "proofcast.toml")
MATCHER = AssetMatcher(CFG.assets)
FEED = RssFeed(name="Example", url="https://feed.example.com/rss")
NOW = datetime(2024, 8, 10, tzinfo=UTC)


# -- mapping -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Apple unveils new iPhone", ["AAPL"]),
        ("AAPL slides premarket", ["AAPL"]),
        ("Shares of $KO rose", ["KO"]),
        ("Coca-Cola (KO) beats", ["KO"]),
        ("NYSE: KO hits record", ["KO"]),
        ("The KO round", []),
        ("A meta-analysis of sleep", []),
        ("Meta to cut costs", ["META"]),
        ("Facebook parent unveils glasses", ["META"]),
        ("Bitcoin and Ether rally", ["BTC", "ETH"]),
        ("JPMorgan Chase earnings", ["JPM"]),
        ("investors chase gains", []),
        ("Pineapple prices", []),
        ("Exxon Mobil Corporation output", ["XOM"]),
        ("Alphabet's Google and Microsoft", ["GOOGL", "MSFT"]),
        ("", []),
    ],
)
def test_asset_matching(text: str, expected: list[str]) -> None:
    assert MATCHER.match(text) == expected


def test_unicode_lookalikes_normalised() -> None:
    zero_width = "A" + chr(0x200B) + "APL"
    assert MATCHER.match(f"{zero_width} jumps") == ["AAPL"]
    full_width = "".join(chr(0xFF00 + ord(c) - 0x20) for c in "AAPL")
    assert MATCHER.match(f"{full_width} jumps") == ["AAPL"]


def test_normalize_text_caps_length() -> None:
    assert len(normalize_text("a" * 100_000)) == 20_000


def test_strip_company_suffix() -> None:
    assert strip_company_suffix("The Coca-Cola Company") == "Coca-Cola"
    assert strip_company_suffix("JPMorgan Chase & Co.") == "JPMorgan Chase"
    assert strip_company_suffix("Meta Platforms, Inc.") == "Meta"


# -- RSS -------------------------------------------------------------------------------


def test_rss_fixture_parsing() -> None:
    items = news.parse_feed(fixture_text("handwritten/rss_sample.xml"), FEED, MATCHER, NOW)
    by_title = {i.title: i for i in items}
    apple = by_title["Apple beats estimates as iPhone sales rise"]
    assert apple.assets == ["AAPL"]
    assert apple.published_at == datetime(2024, 8, 1, 20, 35, tzinfo=UTC)
    assert apple.summary is not None
    assert "<" not in apple.summary and "&amp;" not in apple.summary
    assert "&" in apple.summary
    ko = by_title["Coca-Cola raises dividend"]
    assert ko.assets == ["KO"]
    assert ko.published_at == datetime(2024, 8, 1, 18, 0, tzinfo=UTC)
    assert by_title["Bitcoin slips below $60,000"].assets == ["BTC"]
    # Unrelated items are dropped; undated ones get the fetch time; future dates are clamped.
    assert "A meta-analysis of commuting habits" not in by_title
    assert by_title["Undated Nvidia story"].published_at == NOW
    assert by_title["Tesla item from the future"].published_at == NOW
    # javascript: links are never stored.
    assert all(i.url.startswith("https://") for i in items)
    assert all(i.source == "Example" for i in items)


def test_atom_feed() -> None:
    (item,) = news.parse_feed(fixture_text("handwritten/atom_sample.xml"), FEED, MATCHER, NOW)
    assert item.assets == ["ETH"]
    assert item.published_at == datetime(2024, 8, 5, 9, 0, tzinfo=UTC)


def test_fetch_feed_uses_client() -> None:
    router = Router().file("feed.example.com", "/rss", "handwritten/rss_sample.xml")
    with router.client() as c:
        items = news.fetch_feed(c, FEED, MATCHER, clock=lambda: NOW)
    assert len(items) == 5
    assert "rss" in router.requests[0].headers["Accept"]


@pytest.mark.parametrize("garbage", ["", "not xml at all", "<rss><channel><item>", "\x00\x01\x02"])
def test_malformed_feeds_do_not_crash(garbage: str) -> None:
    assert news.parse_feed(garbage, FEED, MATCHER, NOW) == []


def test_huge_feed_rejected() -> None:
    with pytest.raises(SourceBlockedError):
        news.parse_feed("x" * (news.MAX_FEED_BYTES + 1), FEED, MATCHER, NOW)


def test_clean_text_caps_and_strips() -> None:
    raw = "<script>alert(1)</script><p>" + "word " * 1000 + "</p>"
    out = news.clean_text(raw, 50)
    assert len(out) <= 50
    assert "<" not in out


# -- GDELT -------------------------------------------------------------------------------


def test_gdelt_fixture() -> None:
    router = Router().file(
        "api.gdeltproject.org", "/api/v2/doc/doc", "handwritten/gdelt_artlist.json"
    )
    aapl = CFG.asset("AAPL")
    with router.client() as c:
        items = news.fetch_gdelt(
            c,
            aapl,
            MATCHER,
            datetime(2024, 8, 1, tzinfo=UTC),
            datetime(2024, 8, 6, tzinfo=UTC),
            clock=lambda: NOW,
        )
        assert c.rate_limiter.per_host["api.gdeltproject.org"] >= 5.0
    params = router.requests[0].url.params
    assert params["query"] == (
        '"Apple" (stock OR shares OR earnings OR investors OR analysts) sourcelang:english'
    )
    assert params["startdatetime"] == "20240801000000"
    titles = {i.title: i for i in items}
    assert set(titles) == {
        "Apple shares slide after supplier warning",
        "Apple, Microsoft and Nvidia lead tech selloff",
        "Apple future item",
    }
    selloff = titles["Apple, Microsoft and Nvidia lead tech selloff"]
    assert selloff.assets == ["AAPL", "MSFT", "NVDA"]
    assert selloff.source == "gdelt:markets.example.org"
    assert titles["Apple future item"].published_at == NOW


def test_gdelt_rate_limit_text_is_reported() -> None:
    router = Router().text(
        "api.gdeltproject.org", "/", "Please limit requests to one every 5 seconds"
    )
    with router.client() as c, pytest.raises(SourceBlockedError):
        news.fetch_gdelt(
            c, CFG.asset("AAPL"), MATCHER, datetime(2024, 8, 1, tzinfo=UTC), NOW, clock=lambda: NOW
        )
