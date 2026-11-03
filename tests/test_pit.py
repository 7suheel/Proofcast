"""Point-in-time access: nothing published at or after ``as_of`` may leak through (rule 4)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from helpers import bar, filing, macro, news
from proofcast.models import SourceRef
from proofcast.pit import (
    LookaheadError,
    Snapshot,
    assert_no_lookahead,
    assert_snapshot_clean,
    filter_bars,
    filter_news,
    get_data,
)
from proofcast.storage import DataStore


def _ts(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


def test_news_strictly_before(store: DataStore) -> None:
    as_of = _ts(2024, 3, 1, 12)
    before = news("before", as_of - timedelta(microseconds=1))
    exactly = news("exactly", as_of)
    after = news("after", as_of + timedelta(seconds=1))
    store.write_news([before, exactly, after])
    snap = get_data(store, "AAPL", as_of)
    assert [n.title for n in snap.news] == ["before"]


def test_news_filtered_by_asset(store: DataStore) -> None:
    as_of = _ts(2024, 3, 1)
    store.write_news(
        [
            news("apple", as_of - timedelta(days=1), assets=["AAPL"]),
            news("msft", as_of - timedelta(days=1), assets=["MSFT"]),
            news("both", as_of - timedelta(days=1), assets=["AAPL", "MSFT"]),
        ]
    )
    assert {n.title for n in get_data(store, "AAPL", as_of).news} == {"apple", "both"}


def test_news_lookback(store: DataStore) -> None:
    as_of = _ts(2024, 3, 1)
    store.write_news(
        [news("old", as_of - timedelta(days=40)), news("new", as_of - timedelta(days=2))]
    )
    snap = get_data(store, "AAPL", as_of, news_lookback_days=30)
    assert [n.title for n in snap.news] == ["new"]


def test_stock_bar_of_today_hidden_until_after_close(store: DataStore) -> None:
    today = date(2024, 3, 1)
    store.write_bars("AAPL", [bar(today - timedelta(days=1), 100), bar(today, 101)])
    # During the trading session the day's bar is not complete yet.
    during = get_data(store, "AAPL", _ts(2024, 3, 1, 18))
    assert [b.date for b in during.bars] == [today - timedelta(days=1)]
    # Exactly at the availability time it is still excluded (strictly before).
    at = get_data(store, "AAPL", _ts(2024, 3, 1, 22))
    assert [b.date for b in at.bars] == [today - timedelta(days=1)]
    after = get_data(store, "AAPL", _ts(2024, 3, 1, 22, 0, 1))
    assert after.latest_bar() is not None
    assert after.latest_bar().date == today  # type: ignore[union-attr]


def test_crypto_bar_hidden_until_next_midnight(store: DataStore) -> None:
    day = date(2024, 3, 1)
    store.write_bars("BTC", [bar(day, 60000, asset="BTC", kind="crypto")])
    assert get_data(store, "BTC", _ts(2024, 3, 1, 23, 59)).bars == []
    assert get_data(store, "BTC", _ts(2024, 3, 2, 0, 0)).bars == []
    assert len(get_data(store, "BTC", _ts(2024, 3, 2, 0, 1)).bars) == 1


def test_filings_by_acceptance_time(store: DataStore) -> None:
    as_of = _ts(2024, 2, 1, 21, 30)
    store.write_filings(
        [
            filing("a-1", as_of - timedelta(minutes=1)),
            filing("a-2", as_of),
            filing("a-3", as_of - timedelta(days=1), company="MSFT"),
        ]
    )
    assert [f.id for f in get_data(store, "AAPL", as_of).filings] == ["a-1"]


def test_macro_uses_publication_not_period(store: DataStore) -> None:
    # January CPI (period starts 2024-01-01) is only published mid-February.
    pub = _ts(2024, 2, 13, 13, 30)
    store.write_macro("CPIAUCSL", [macro("CPIAUCSL", date(2024, 1, 1), 308.4, pub)])
    early = get_data(store, "AAPL", _ts(2024, 2, 1), macro_series=["CPIAUCSL"])
    assert early.macro["CPIAUCSL"] == []
    late = get_data(store, "AAPL", pub + timedelta(seconds=1), macro_series=["CPIAUCSL"])
    assert len(late.macro["CPIAUCSL"]) == 1


def test_naive_as_of_rejected(store: DataStore) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        get_data(store, "AAPL", datetime(2024, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        filter_news([], datetime(2024, 1, 1))


def test_assert_no_lookahead_raises() -> None:
    as_of = _ts(2024, 1, 1)
    ok = news("ok", as_of - timedelta(seconds=1))
    bad = news("bad", as_of)
    assert_no_lookahead([ok], as_of)
    with pytest.raises(LookaheadError, match="not before"):
        assert_no_lookahead([ok, bad], as_of)


def test_assert_no_lookahead_on_source_refs() -> None:
    as_of = _ts(2024, 1, 1)
    ref = SourceRef(id="x", kind="news", title="t", published_at=as_of + timedelta(days=1))
    with pytest.raises(LookaheadError):
        assert_no_lookahead([ref], as_of)


def test_snapshot_check_catches_hand_built_leak() -> None:
    as_of = _ts(2024, 1, 2)
    leaked = Snapshot(asset="AAPL", as_of=as_of, bars=[bar(date(2024, 1, 2), 10)])
    with pytest.raises(LookaheadError):
        assert_snapshot_clean(leaked)


def test_filter_bars_sorted() -> None:
    bars = [bar(date(2024, 1, 3), 3), bar(date(2024, 1, 1), 1), bar(date(2024, 1, 2), 2)]
    out = filter_bars(bars, _ts(2024, 1, 10))
    assert [b.close for b in out] == [1, 2, 3]


@pytest.mark.parametrize("offset_seconds", [-86400, -1, 0, 1, 86400])
def test_boundary_property(store: DataStore, offset_seconds: int) -> None:
    """For any as_of, every returned item is strictly earlier, and nothing earlier is lost."""
    base = _ts(2024, 5, 1, 12)
    items = [news(f"n{i}", base + timedelta(hours=i)) for i in range(-48, 49, 6)]
    store.write_news(items)
    as_of = base + timedelta(seconds=offset_seconds)
    got = get_data(store, "AAPL", as_of).news
    assert all(n.published_at < as_of for n in got)
    assert len(got) == sum(1 for n in items if n.published_at < as_of)
