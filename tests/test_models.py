from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from helpers import FETCHED, bar
from proofcast.models import (
    Asset,
    Drawdown,
    Forecast,
    NewsItem,
    PriceBar,
    ResolutionRule,
    bar_available_at,
    stable_id,
)


def _rule() -> ResolutionRule:
    return ResolutionRule(
        asset="BTC",
        reference_date=date(2024, 1, 1),
        reference_close=100,
        target_date=date(2024, 1, 8),
    )


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        NewsItem(
            id="x",
            title="t",
            url="https://e.com",
            source="s",
            published_at=datetime(2024, 1, 1),
            fetched_at=FETCHED,
        )


def test_datetimes_normalised_to_utc() -> None:
    plus3 = timezone(timedelta(hours=3))
    item = NewsItem(
        id="x",
        title="t",
        url="https://e.com",
        source="s",
        published_at=datetime(2024, 1, 1, 15, 0, tzinfo=plus3),
        fetched_at=FETCHED,
    )
    assert item.published_at == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert item.published_at.utcoffset() == timedelta(0)


def test_bar_available_at_stock_is_after_close_in_all_seasons() -> None:
    winter = bar_available_at(date(2024, 1, 10), "stock")
    summer = bar_available_at(date(2024, 7, 10), "stock")
    # NYSE closes 21:00 UTC in winter and 20:00 UTC in summer.
    assert winter > datetime(2024, 1, 10, 21, 0, tzinfo=UTC)
    assert summer > datetime(2024, 7, 10, 20, 0, tzinfo=UTC)
    assert winter.date() == date(2024, 1, 10)


def test_bar_available_at_crypto_is_next_utc_midnight() -> None:
    assert bar_available_at(date(2024, 1, 10), "crypto") == datetime(2024, 1, 11, tzinfo=UTC)


def test_price_bar_rejects_inconsistent_ohlc() -> None:
    with pytest.raises(ValidationError):
        PriceBar(
            asset="AAPL",
            date=date(2024, 1, 1),
            open=10,
            high=9,
            low=8,
            close=12,
            volume=1,
            source="t",
            fetched_at=FETCHED,
            available_at=FETCHED,
        )


def test_price_bar_rejects_non_positive() -> None:
    with pytest.raises(ValidationError):
        bar(date(2024, 1, 1), 0.0)


def test_asset_symbol_validation() -> None:
    Asset(symbol="BTC", kind="crypto", name="Bitcoin")
    with pytest.raises(ValidationError):
        Asset(symbol="../etc", kind="stock", name="x")
    with pytest.raises(ValidationError):
        Asset(symbol="AAPL", kind="stock", name="x", cik="320193")


def test_models_are_frozen() -> None:
    b = bar(date(2024, 1, 1), 10)
    with pytest.raises(ValidationError):
        b.close = 11  # type: ignore[misc]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Asset.model_validate({"symbol": "X", "kind": "stock", "name": "x", "evil": 1})


def test_stable_id_deterministic() -> None:
    assert stable_id("a", "b") == stable_id("a", "b")
    assert stable_id("a", "b") != stable_id("ab")
    assert len(stable_id("x")) == 16


def test_forecast_probability_bounds() -> None:
    common = {
        "id": "f1",
        "created_at": FETCHED,
        "asset": "BTC",
        "question": "q",
        "horizon_days": 7,
        "base_rate": 0.5,
        "resolution_rule": _rule(),
        "setup": "baseline:test",
        "is_baseline": True,
    }
    Forecast.model_validate({**common, "probability": 0.6})
    with pytest.raises(ValidationError):
        Forecast.model_validate({**common, "probability": 1.2})
    with pytest.raises(ValidationError):
        Forecast.model_validate({**common, "probability": -0.1})


def test_forecast_json_roundtrip() -> None:
    f = Forecast(
        id="f1",
        created_at=FETCHED,
        asset="BTC",
        question="q",
        horizon_days=7,
        probability=0.6,
        base_rate=0.52,
        resolution_rule=_rule(),
        setup="baseline:test",
        is_baseline=True,
    )
    assert Forecast.model_validate_json(f.model_dump_json()) == f


def test_drawdown_id() -> None:
    d = Drawdown(
        asset="AAPL",
        start=date(2020, 2, 12),
        trough=date(2020, 3, 23),
        recovery=None,
        peak_close=80,
        trough_close=56,
        depth_pct=30,
    )
    assert d.id == "AAPL-2020-02-12"
