from datetime import UTC, date, datetime

import pytest

from conftest import REPO_ROOT
from mock_http import Router
from proofcast.adapters import fred, sec
from proofcast.config import MacroSeries, load_config

CFG = load_config(REPO_ROOT / "proofcast.toml")
AAPL = CFG.asset("AAPL")
UA = {"PROOFCAST_SEC_USER_AGENT": "Proofcast tests admin@example.org"}
NOW = datetime(2024, 11, 2, tzinfo=UTC)


def test_sec_requires_user_agent_with_email() -> None:
    with pytest.raises(sec.SecUserAgentError):
        sec.sec_user_agent({})
    with pytest.raises(sec.SecUserAgentError):
        sec.sec_user_agent({"PROOFCAST_SEC_USER_AGENT": "Proofcast no email"})
    assert sec.sec_user_agent(UA) == UA["PROOFCAST_SEC_USER_AGENT"]


def test_sec_submissions_fixture() -> None:
    router = Router().file(
        "data.sec.gov", "/submissions/CIK0000320193.json", "handwritten/sec_submissions_aapl.json"
    )
    with router.client() as c:
        filings = sec.fetch_filings(c, AAPL, environ=UA, clock=lambda: NOW, since=date(2020, 1, 1))
    assert router.requests[0].headers["User-Agent"] == UA["PROOFCAST_SEC_USER_AGENT"]
    by_id = {f.id: f for f in filings}
    # Form 4 is filtered out; the 2014 filing is before `since`.
    assert set(by_id) == {"0000320193-24-000123", "0000320193-24-000120", "0000320193-24-000081"}
    eight_k = by_id["0000320193-24-000120"]
    assert eight_k.form_type == "8-K"
    assert eight_k.items == ["2.02", "9.01"]
    assert eight_k.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000120/aapl-20241031.htm"
    )
    # 16:30:41 on the EDGAR clock, read conservatively as EST (UTC-5).
    assert eight_k.filed_at == datetime(2024, 10, 31, 21, 30, 41, tzinfo=UTC)


def test_acceptance_time_is_never_earlier_than_utc_reading() -> None:
    raw = "2024-07-01T12:00:00.000Z"
    assert sec.parse_acceptance(raw) >= datetime(2024, 7, 1, 12, tzinfo=UTC)


def test_sec_rate_limit_configured() -> None:
    router = Router().file("data.sec.gov", "/", "handwritten/sec_submissions_aapl.json")
    with router.client() as c:
        sec.fetch_filings(c, AAPL, environ=UA, clock=lambda: NOW)
        assert c.rate_limiter.per_host["data.sec.gov"] >= 0.1


def test_company_facts_fixture() -> None:
    router = Router().file(
        "data.sec.gov", "/api/xbrl/companyfacts/", "handwritten/sec_companyfacts_aapl.json"
    )
    with router.client() as c:
        facts = sec.fetch_company_facts(c, AAPL, environ=UA, since=date(2020, 1, 1))
    concepts = {(f.concept, f.period_end.isoformat(), f.filed.isoformat()) for f in facts}
    assert ("NetIncomeLoss", "2024-06-29", "2024-08-02") in concepts
    # The same period repeated in a later filing keeps its first filing date.
    assert ("NetIncomeLoss", "2024-06-29", "2024-11-01") not in concepts
    assert ("EarningsPerShareDiluted", "2024-09-28", "2024-11-01") in concepts
    assert all(f.concept != "SomethingElse" for f in facts)
    assert all(f.filed >= date(2020, 1, 1) for f in facts)
    eps = next(f for f in facts if f.concept == "EarningsPerShareDiluted")
    assert eps.published_at > datetime(2024, 11, 1, 23, 59, tzinfo=UTC)


def test_stock_without_cik_returns_nothing() -> None:
    asset = AAPL.model_copy(update={"cik": None})
    with Router().client() as c:
        assert sec.fetch_filings(c, asset, environ=UA) == []


# ---------------------------------------------------------------------------
# FRED
# ---------------------------------------------------------------------------

CPI = MacroSeries(series_id="CPIAUCSL", name="CPI", frequency="monthly", release_lag_days=16)
DGS10 = MacroSeries(series_id="DGS10", name="10y", frequency="daily", release_lag_days=1)
UNRATE = MacroSeries(series_id="UNRATE", name="U", frequency="monthly", release_lag_days=10)


def test_fred_csv_recorded_without_key() -> None:
    router = Router().file(
        "fred.stlouisfed.org", "/graph/fredgraph.csv", "recorded/fredgraph_cpiaucsl.csv"
    )
    with router.client() as c:
        obs = fred.fetch_macro(c, CPI, environ={}, clock=lambda: NOW)
    assert "api_key" not in str(router.requests[0].url)
    jan = next(o for o in obs if o.date == date(2024, 1, 1))
    assert jan.value == pytest.approx(309.698)
    assert jan.published_at_estimated
    # End of January + 16 days.
    assert jan.published_at == datetime(2024, 2, 16, 23, 59, tzinfo=UTC)
    assert all(o.published_at.date() > o.date for o in obs)


def test_fred_csv_skips_missing_values() -> None:
    router = Router().file("fred.stlouisfed.org", "/", "recorded/fredgraph_dgs10.csv")
    with router.client() as c:
        obs = fred.fetch_macro(c, DGS10, environ={}, clock=lambda: NOW)
    assert date(2024, 7, 4) not in {o.date for o in obs}
    assert len(obs) == 9


def test_fred_api_with_key_uses_first_release_dates() -> None:
    router = Router().file(
        "api.stlouisfed.org", "/fred/series/observations", "handwritten/fred_api_unrate.json"
    )
    with router.client() as c:
        obs = fred.fetch_macro(c, UNRATE, environ={"FRED_API_KEY": "k123"}, clock=lambda: NOW)
    params = router.requests[0].url.params
    assert params["output_type"] == "4"
    assert params["api_key"] == "k123"
    by_date = {o.date: o for o in obs}
    assert date(2024, 3, 1) not in by_date  # "." value
    jan = by_date[date(2024, 1, 1)]
    assert not jan.published_at_estimated
    assert jan.published_at == datetime(2024, 2, 2, 23, 59, tzinfo=UTC)
    # A "release" before the period ended is impossible: fall back to the estimate.
    apr = by_date[date(2024, 4, 1)]
    assert apr.published_at_estimated
    assert apr.published_at == datetime(2024, 5, 10, 23, 59, tzinfo=UTC)


def test_fred_html_detected() -> None:
    from proofcast.net.http import SourceBlockedError

    with pytest.raises(SourceBlockedError):
        fred.parse_fred_csv(CPI, "<html></html>", NOW)


def test_period_end() -> None:
    assert fred.period_end(date(2024, 2, 1), "monthly") == date(2024, 2, 29)
    assert fred.period_end(date(2024, 2, 5), "daily") == date(2024, 2, 5)


# -- fixes after the first scheduled run (2026-11-04) -------------------------------------

DAILY_API = (
    '{"observations": ['
    '{"realtime_start": "2026-11-04", "realtime_end": "2026-11-04", "date": "2026-10-30", '
    '"value": "4.11"},'
    '{"realtime_start": "2026-11-04", "realtime_end": "2026-11-04", "date": "2026-11-02", '
    '"value": "."}'
    "]}"
)


def test_fred_daily_series_skip_vintages() -> None:
    router = Router().text("api.stlouisfed.org", "/fred/series/observations", DAILY_API)
    with router.client() as c:
        obs = fred.fetch_macro(c, DGS10, environ={"FRED_API_KEY": "k123"}, clock=lambda: NOW)
    params = router.requests[0].url.params
    assert "output_type" not in params and "realtime_start" not in params
    (o,) = obs
    assert o.source == "fred-api"
    # realtime_start is only the request date here, so the release time is estimated.
    assert o.published_at_estimated
    assert o.published_at == datetime(2026, 10, 31, 23, 59, tzinfo=UTC)


def test_fred_api_failure_falls_back_to_csv() -> None:
    body = '{"error_code": 400, "error_message": "Bad Request. Too many vintage dates."}'
    router = (
        Router()
        .text("api.stlouisfed.org", "/", body, status=400)
        .file("fred.stlouisfed.org", "/", "recorded/fredgraph_cpiaucsl.csv")
    )
    notes: list[str] = []
    with router.client() as c:
        obs = fred.fetch_macro(
            c, CPI, environ={"FRED_API_KEY": "k123"}, clock=lambda: NOW, on_fallback=notes.append
        )
    assert obs and all(o.source == "fred-csv" for o in obs)
    assert len(notes) == 1
    assert "Too many vintage dates" in notes[0]
    assert "k123" not in notes[0]
