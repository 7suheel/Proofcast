from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from helpers import T0, bar, filing, macro, news, series
from proofcast.storage import DataStore, atomic_write_text


def test_bars_roundtrip_sorted(store: DataStore) -> None:
    bars = series([10, 11, 12.5, 13.25])
    assert store.write_bars("AAPL", reversed(bars)) == 4
    assert store.read_bars("AAPL") == bars


def test_bars_rewrite_is_idempotent(store: DataStore) -> None:
    bars = series([10, 11, 12])
    store.write_bars("AAPL", bars)
    before = store.prices_path("AAPL").read_bytes()
    assert store.write_bars("AAPL", bars) == 0
    assert store.prices_path("AAPL").read_bytes() == before


def test_bars_merge_newer_wins(store: DataStore) -> None:
    store.write_bars("AAPL", series([10, 11, 12]))
    fixed = bar(date(2020, 1, 2), 11.5)
    assert store.write_bars("AAPL", [fixed]) == 1
    assert [b.close for b in store.read_bars("AAPL")] == [10, 11.5, 12]


def test_bars_wrong_asset_rejected(store: DataStore) -> None:
    with pytest.raises(ValueError, match="written to"):
        store.write_bars("MSFT", series([10]))


def test_bars_csv_uses_lf_and_header(store: DataStore) -> None:
    store.write_bars("AAPL", series([10, 11]))
    raw = store.prices_path("AAPL").read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b"date,open,high,low,close,volume,source,fetched_at,available_at\n")


def test_missing_files_read_empty(store: DataStore) -> None:
    assert store.read_bars("AAPL") == []
    assert store.read_news() == []
    assert store.read_filings() == []
    assert store.read_macro("CPIAUCSL") == []


@pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", ".hidden", "", "C:x"])
def test_unsafe_names_rejected(store: DataStore, name: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        store.prices_path(name)


def test_news_grouped_by_month_and_deduplicated(store: DataStore) -> None:
    a = news("A", datetime(2024, 1, 31, 23, 0, tzinfo=UTC))
    b = news("B", datetime(2024, 2, 1, 1, 0, tzinfo=UTC))
    assert store.write_news([a, b, a]) == 2
    files = sorted(p.name for p in (store.root / "news").iterdir())
    assert files == ["2024-01.jsonl", "2024-02.jsonl"]
    assert store.write_news([a]) == 0
    assert store.read_news() == [a, b]


def test_news_duplicate_merges_asset_tags(store: DataStore) -> None:
    a = news("Apple and Microsoft", T0, assets=["AAPL"], url="https://e.com/1")
    a2 = news("Apple and Microsoft", T0, assets=["MSFT"], url="https://e.com/1")
    store.write_news([a])
    assert store.write_news([a2]) == 1
    (only,) = store.read_news()
    assert only.assets == ["AAPL", "MSFT"]


def test_prune_news_text_keeps_metadata(store: DataStore) -> None:
    now = datetime(2024, 6, 1, tzinfo=UTC)
    old = news("old", now - timedelta(days=91), summary="long text")
    recent = news("recent", now - timedelta(days=10), summary="keep me")
    store.write_news([old, recent])
    assert store.prune_news_text(now) == 1
    by_title = {n.title: n for n in store.read_news()}
    assert by_title["old"].summary is None
    assert by_title["old"].url == old.url
    assert by_title["old"].published_at == old.published_at
    assert by_title["recent"].summary == "keep me"
    assert store.prune_news_text(now) == 0


def test_filings_roundtrip(store: DataStore) -> None:
    f1 = filing("0000320193-24-000001", datetime(2024, 2, 1, 21, 30, tzinfo=UTC))
    f2 = filing("0000320193-24-000002", datetime(2024, 1, 5, 12, 0, tzinfo=UTC), form_type="10-Q")
    assert store.write_filings([f1, f2]) == 2
    assert store.read_filings() == [f2, f1]
    assert store.write_filings([f1]) == 0


def test_macro_roundtrip_and_revision(store: DataStore) -> None:
    pub = datetime(2024, 2, 13, 13, 30, tzinfo=UTC)
    o = macro("CPIAUCSL", date(2024, 1, 1), 308.4, pub)
    assert store.write_macro("CPIAUCSL", [o]) == 1
    assert store.read_macro("CPIAUCSL") == [o]
    revised = macro("CPIAUCSL", date(2024, 1, 1), 308.5, pub + timedelta(days=30))
    assert store.write_macro("CPIAUCSL", [revised]) == 1
    (stored,) = store.read_macro("CPIAUCSL")
    assert stored.value == 308.5
    # The observation keeps the publication time it was first stored with.
    assert stored.published_at == pub


def test_macro_real_release_time_replaces_estimate(store: DataStore) -> None:
    est = macro("UNRATE", date(2024, 1, 1), 3.7, datetime(2024, 2, 11, tzinfo=UTC))
    real = macro(
        "UNRATE", date(2024, 1, 1), 3.7, datetime(2024, 2, 2, 13, 30, tzinfo=UTC), estimated=False
    )
    store.write_macro("UNRATE", [est])
    assert store.write_macro("UNRATE", [real]) == 1
    (stored,) = store.read_macro("UNRATE")
    assert not stored.published_at_estimated
    assert stored.published_at == real.published_at
    # ...but an estimate never replaces a real release time.
    assert store.write_macro("UNRATE", [est]) == 0


def test_invalid_jsonl_reports_location(store: DataStore) -> None:
    path = store.root / "news" / "2024-01.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"2024-01.jsonl:1"):
        store.read_news()


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "f.txt"
    atomic_write_text(target, "hello\n")
    atomic_write_text(target, "world\n")
    assert target.read_text(encoding="utf-8") == "world\n"
    assert [p.name for p in target.parent.iterdir()] == ["f.txt"]
