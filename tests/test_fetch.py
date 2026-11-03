from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from mock_http import Router
from proofcast import fetch as fetch_mod
from proofcast.cli import app
from proofcast.config import load_config
from proofcast.fetch import Fetcher
from proofcast.net.http import HttpClient
from proofcast.storage import DataStore

NOW = datetime(2024, 6, 10, tzinfo=UTC)


def _router() -> Router:
    return (
        Router()
        .file("stooq.com", "/", "handwritten/stooq_blocked.html")
        .file("query1.finance.yahoo.com", "/", "recorded/yahoo_aapl_chart.json")
        .file("api.exchange.coinbase.com", "/", "recorded/coinbase_btc_candles.json")
        .file("fred.stlouisfed.org", "/", "recorded/fredgraph_cpiaucsl.csv")
        .file("data.sec.gov", "/submissions/", "handwritten/sec_submissions_aapl.json")
        .file("data.sec.gov", "/api/xbrl/", "handwritten/sec_companyfacts_aapl.json")
        .file("www.cnbc.com", "/", "handwritten/rss_sample.xml")
        .file("www.coindesk.com", "/", "handwritten/atom_sample.xml")
        .text("cointelegraph.com", "/", "<html>maintenance</html>", status=503)
        .file("www.sec.gov", "/news/", "handwritten/rss_sample.xml")
    )


def _fetcher(store: DataStore, router: Router, environ: dict[str, str]) -> Fetcher:
    cfg = load_config(REPO_ROOT / "proofcast.toml")
    return Fetcher(cfg, store, router.client(), environ=environ, clock=lambda: NOW)


def test_prices_chain_and_store(store: DataStore) -> None:
    router = _router()
    f = _fetcher(store, router, {})
    report = f.prices([f.cfg.asset("AAPL")], since=date(2024, 6, 1))
    assert report.added["prices/AAPL"] == 5
    assert any("stooq" in w for w in report.warnings)
    assert store.read_bars("AAPL")[-1].source == "yahoo-unofficial"
    # Second run: nothing changes.
    assert f.prices([f.cfg.asset("AAPL")], since=date(2024, 6, 1)).added["prices/AAPL"] == 0


def test_filings_skipped_without_user_agent(store: DataStore) -> None:
    router = _router()
    report = _fetcher(store, router, {}).filings(
        [load_config(REPO_ROOT / "proofcast.toml").asset("AAPL")]
    )
    assert report.warnings and "PROOFCAST_SEC_USER_AGENT" in report.warnings[0]
    assert router.requests == []


def test_filings_with_user_agent(store: DataStore) -> None:
    router = _router()
    f = _fetcher(store, router, {"PROOFCAST_SEC_USER_AGENT": "Proofcast t admin@example.org"})
    report = f.filings([f.cfg.asset("AAPL")], since=date(2020, 1, 1))
    assert report.added["filings/AAPL"] == 3
    assert report.added["fundamentals/AAPL"] >= 2
    assert len(store.read_facts("AAPL")) == report.added["fundamentals/AAPL"]


def test_macro_without_key_warns(store: DataStore) -> None:
    f = _fetcher(store, _router(), {})
    report = f.macro()
    assert any("FRED_API_KEY" in w for w in report.warnings)
    assert report.added["macro/CPIAUCSL"] > 0


def test_news_partial_failure_still_stores(store: DataStore) -> None:
    f = _fetcher(store, _router(), {})
    report = f.news(list(f.cfg.assets))
    assert report.added["news"] >= 5
    assert any("Cointelegraph" in e for e in report.errors)
    assert any("ETH" in n.assets for n in store.read_news())


def test_cli_fetch_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_text = (REPO_ROOT / "proofcast.toml").read_text(encoding="utf-8")
    (tmp_path / "proofcast.toml").write_text(cfg_text, encoding="utf-8")
    router = _router()
    monkeypatch.setattr(fetch_mod, "make_client", lambda cfg, use_cache=True: router.client())
    monkeypatch.delenv("PROOFCAST_SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "fetch",
            "all",
            "-a",
            "AAPL",
            "--since",
            "2024-06-01",
            "--config",
            str(tmp_path / "proofcast.toml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "prices/AAPL: 5" in result.output
    assert "PROOFCAST_SEC_USER_AGENT" in result.output
    assert (tmp_path / "data" / "prices" / "AAPL.csv").exists()


def test_cli_rejects_unknown_asset_and_bad_date() -> None:
    runner = CliRunner()
    cfg = str(REPO_ROOT / "proofcast.toml")
    assert runner.invoke(app, ["fetch", "prices", "-a", "DOGE", "--config", cfg]).exit_code == 2
    assert (
        runner.invoke(app, ["fetch", "prices", "--since", "yesterday", "--config", cfg]).exit_code
        == 2
    )


def test_make_client_has_cache(tmp_path: Path) -> None:
    cfg = load_config(REPO_ROOT / "proofcast.toml")
    client = fetch_mod.make_client(cfg)
    assert isinstance(client, HttpClient)
    assert client.cache is not None
    client.close()
