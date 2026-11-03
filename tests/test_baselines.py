from datetime import UTC, date, datetime, timedelta

import pytest

from helpers import series
from proofcast.forecast.baselines import (
    base_rate,
    clamp,
    compute_baselines,
    make_rule,
    momentum,
    question_text,
    reference_bar,
)
from proofcast.forecast.indicators import compute_indicators, rsi

START = date(2020, 1, 1)


def _ts(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=hour)


def test_reference_bar_is_last_complete_bar() -> None:
    bars = series([10, 11, 12], start=START)
    assert reference_bar(bars, _ts(date(2020, 1, 3), 12)).date == date(2020, 1, 2)  # type: ignore[union-attr]
    assert reference_bar(bars, _ts(date(2020, 1, 3), 23)).date == date(2020, 1, 3)  # type: ignore[union-attr]
    assert reference_bar(bars, _ts(date(2019, 1, 1))) is None


def test_rule_and_question() -> None:
    ref = series([10, 11], start=START)[-1]
    rule = make_rule(ref, 7)
    assert rule.reference_date == date(2020, 1, 2)
    assert rule.target_date == date(2020, 1, 9)
    assert rule.reference_close == 11
    q = question_text(rule)
    assert "first trading day on or after 2020-01-09" in q and "11" in q


def test_base_rate_rising_and_falling() -> None:
    up = series([100 + i for i in range(200)], start=START)
    down = series([300 - i for i in range(200)], start=START)
    t = _ts(START + timedelta(days=300))
    r_up, n = base_rate(up, 7, t)
    r_down, _ = base_rate(down, 7, t)
    assert n == 193
    assert r_up == pytest.approx((193 + 1) / (193 + 2))
    assert r_down == pytest.approx(1 / 195)


def test_base_rate_only_uses_outcomes_known_before_t() -> None:
    bars = series([100 + i for i in range(100)], start=START)
    t = _ts(START + timedelta(days=50))  # bars after day 49 are not public yet
    _, n = base_rate(bars, 7, t)
    # Days 0..42 have their 7-day outcome by day 49; later outcomes are unknown at T.
    assert n == 43
    assert base_rate([], 7, t) == (0.5, 0)


def test_base_rate_lookback() -> None:
    bars = series([100 + i for i in range(800)], start=START)
    _, n_all = base_rate(bars, 7, _ts(START + timedelta(days=900)), lookback_years=10)
    _, n_1y = base_rate(bars, 7, _ts(START + timedelta(days=900)), lookback_years=1)
    assert n_1y < n_all


def test_momentum_direction_and_clamp() -> None:
    t = _ts(START + timedelta(days=400))
    up = series([100 * 1.002**i for i in range(300)], start=START)
    down = series([100 * 0.998**i for i in range(300)], start=START)
    assert momentum(up, 30, t, 0.5) > 0.5
    assert momentum(down, 30, t, 0.5) < 0.5
    assert momentum(up, 30, t, 0.99) == 0.95
    assert momentum(series([100, 100], start=START), 30, t, 0.5) == 0.5
    assert clamp(1.2) == 0.95 and clamp(-1) == 0.05


def test_compute_baselines() -> None:
    bars = series([100 + (i % 10) for i in range(300)], start=START)
    b = compute_baselines(bars, 7, _ts(START + timedelta(days=400)))
    assert b.always_50 == 0.5
    assert 0 < b.base_rate < 1
    assert set(b.as_dict()) == {"always_50", "base_rate", "momentum"}


def test_indicators() -> None:
    bars = series([100 + i for i in range(260)], start=START)
    ind = {i.id: i for i in compute_indicators(bars, "stock")}
    assert {"I:ret_7d", "I:ret_30d", "I:vs_sma50", "I:vs_sma200", "I:rsi14"} <= set(ind)
    assert ind["I:rsi14"].value == 100.0
    assert ind["I:ret_7d"].value > 0
    assert ind["I:from_52w_high"].value == 0
    assert compute_indicators(bars[:1], "stock") == []
    assert rsi([1, 2]) is None
