from datetime import UTC, date, datetime

import pytest

from conftest import REPO_ROOT
from mock_http import Router, fixture_text
from proofcast.adapters import prices
from proofcast.config import load_config
from proofcast.net.http import SourceBlockedError

CFG = load_config(REPO_ROOT / "proofcast.toml")
BTC = CFG.asset("BTC")
AAPL = CFG.asset("AAPL")


def clock_at(*args: int):  # type: ignore[no-untyped-def]
    return lambda: datetime(*args, tzinfo=UTC)


def test_coinbase_recorded_fixture() -> None:
    router = Router().file(
        "api.exchange.coinbase.com",
        "/products/BTC-USD/candles",
        "recorded/coinbase_btc_candles.json",
    )
    with router.client() as c:
        bars = prices.fetch_coinbase(
            c, BTC, date(2024, 3, 1), date(2024, 3, 6), clock=clock_at(2024, 3, 6, 12)
        )
    # The 2024-03-06 candle was still open at fetch time and is dropped.
    assert [b.date for b in bars] == [date(2024, 3, d) for d in range(1, 6)]
    first = bars[0]
    assert (first.open, first.high, first.low, first.close) == (
        61179.03,
        63267.18,
        60788.33,
        62436.72,
    )
    assert first.source == "coinbase"
    assert first.available_at == datetime(2024, 3, 2, tzinfo=UTC)
    req = router.requests[0]
    assert req.url.params["granularity"] == "86400"


def test_coinbase_paginates_300_days() -> None:
    router = Router().text("api.exchange.coinbase.com", "/", "[]")
    with router.client() as c:
        prices.fetch_coinbase(
            c, BTC, date(2023, 1, 1), date(2024, 12, 31), clock=clock_at(2025, 1, 1)
        )
    assert len(router.requests) == 3
    assert router.requests[1].url.params["start"] == "2023-10-28T00:00:00Z"


def test_coingecko_recorded_fixture() -> None:
    router = Router().file(
        "api.coingecko.com",
        "/api/v3/coins/bitcoin/market_chart",
        "recorded/coingecko_btc_market_chart.json",
    )
    with router.client() as c:
        bars = prices.fetch_coingecko(c, BTC, 5, clock=clock_at(2026, 11, 3, 12), environ={})
    assert [b.date for b in bars] == [
        date(2026, 10, 29),
        date(2026, 10, 30),
        date(2026, 10, 31),
        date(2026, 11, 1),
        date(2026, 11, 2),
    ]
    assert all(b.source == "coingecko:close-only" for b in bars)
    assert all(b.open == b.close == b.high == b.low for b in bars)
    assert "x-cg-demo-api-key" not in router.requests[0].headers


def test_coingecko_demo_key_header() -> None:
    router = Router().text("api.coingecko.com", "/", '{"prices": []}')
    with router.client() as c:
        prices.fetch_coingecko(
            c, BTC, 5, clock=clock_at(2026, 11, 3), environ={"COINGECKO_DEMO_API_KEY": "demo"}
        )
    assert router.requests[0].headers["x-cg-demo-api-key"] == "demo"
    assert "demo" not in str(router.requests[0].url)


def test_stooq_handwritten_csv() -> None:
    router = Router().file("stooq.com", "/q/d/l/", "handwritten/stooq_aapl.csv")
    with router.client() as c:
        bars = prices.fetch_stooq(c, AAPL, date(2024, 6, 1), clock=clock_at(2024, 6, 10))
    assert len(bars) == 5  # the malformed row is skipped
    assert bars[-1].close == 196.89
    assert router.requests[0].url.params["s"] == "aapl.us"


def test_stooq_bot_check_detected() -> None:
    with pytest.raises(SourceBlockedError, match="bot check"):
        prices.parse_stooq_csv(
            AAPL, fixture_text("handwritten/stooq_blocked.html"), datetime.now(UTC)
        )


def test_stooq_no_data() -> None:
    with pytest.raises(prices.PriceSourceError):
        prices.parse_stooq_csv(AAPL, "No data", datetime.now(UTC))


def test_yahoo_recorded_fixture() -> None:
    router = Router().file(
        "query1.finance.yahoo.com", "/v8/finance/chart/AAPL", "recorded/yahoo_aapl_chart.json"
    )
    with router.client() as c:
        bars = prices.fetch_yahoo(c, AAPL, date(2024, 6, 1), clock=clock_at(2024, 6, 10))
    assert [b.date for b in bars] == [date(2024, 6, d) for d in (3, 4, 5, 6, 7)]
    assert bars[0].close == pytest.approx(194.03, abs=1e-4)
    assert bars[0].source == "yahoo-unofficial"


def test_yahoo_error_payload() -> None:
    with pytest.raises(prices.PriceSourceError):
        prices.parse_yahoo_chart(
            AAPL, {"chart": {"result": None, "error": {"code": "Not Found"}}}, datetime.now(UTC)
        )


def test_partial_bar_dropped_for_stocks() -> None:
    router = Router().file("stooq.com", "/", "handwritten/stooq_aapl.csv")
    with router.client() as c:
        # Fetched during the 2024-06-07 session: that bar is not final yet.
        bars = prices.fetch_stooq(c, AAPL, date(2024, 6, 1), clock=clock_at(2024, 6, 7, 18))
    assert bars[-1].date == date(2024, 6, 6)


def test_chain_falls_back_from_blocked_stooq_to_yahoo() -> None:
    router = (
        Router()
        .file("stooq.com", "/", "handwritten/stooq_blocked.html")
        .file("query1.finance.yahoo.com", "/", "recorded/yahoo_aapl_chart.json")
    )
    with router.client() as c:
        bars, errors = prices.fetch_prices(
            c,
            AAPL,
            date(2024, 6, 1),
            ["stooq", "yahoo"],
            allow_unofficial=True,
            clock=clock_at(2024, 6, 10),
        )
    assert len(bars) == 5
    assert errors and "stooq" in errors[0]


def test_chain_respects_unofficial_flag() -> None:
    router = Router().file("stooq.com", "/", "handwritten/stooq_blocked.html")
    with router.client() as c:
        bars, errors = prices.fetch_prices(
            c,
            AAPL,
            date(2024, 6, 1),
            ["stooq", "yahoo"],
            allow_unofficial=False,
            clock=clock_at(2024, 6, 10),
        )
    assert bars == []
    assert any("unofficial" in e for e in errors)
    assert all(r.url.host != "query1.finance.yahoo.com" for r in router.requests)
