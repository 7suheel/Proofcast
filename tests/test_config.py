from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import REPO_ROOT
from proofcast.config import Config, load_config


def test_repo_config_loads() -> None:
    cfg = load_config(REPO_ROOT / "proofcast.toml")
    assert cfg.forecast.horizons == [7, 30]
    assert {"BTC", "ETH", "SOL"} <= set(cfg.symbols())
    stocks = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "XOM", "KO"}
    assert stocks <= set(cfg.symbols())
    assert {m.series_id for m in cfg.macro} >= {"FEDFUNDS", "CPIAUCSL", "DGS10", "UNRATE"}
    for a in cfg.assets:
        if a.kind == "stock":
            assert a.cik and a.stooq_symbol and a.yahoo_symbol
        else:
            assert a.coingecko_id and a.coinbase_product
    assert cfg.root == REPO_ROOT


def test_asset_lookup_case_insensitive() -> None:
    cfg = load_config(REPO_ROOT / "proofcast.toml")
    assert cfg.asset("btc").symbol == "BTC"
    with pytest.raises(KeyError):
        cfg.asset("DOGE")


def test_paths_relative_to_config(tmp_path: Path) -> None:
    p = tmp_path / "proofcast.toml"
    p.write_text('[project]\ndata_dir = "d"\nledger_path = "l/x.jsonl"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.data_dir == tmp_path.resolve() / "d"
    assert cfg.ledger_path == tmp_path.resolve() / "l" / "x.jsonl"


def test_env_var_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "custom.toml"
    p.write_text("[forecast]\nhorizons = [3]\n", encoding="utf-8")
    monkeypatch.setenv("PROOFCAST_CONFIG", str(p))
    assert load_config().forecast.horizons == [3]


def test_defaults_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROOFCAST_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.assets == []
    assert cfg.forecast.horizons == [7, 30]


def test_invalid_probability_bounds() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"forecast": {"min_probability": 0.6}})


def test_duplicate_assets_rejected() -> None:
    a = {"symbol": "X", "kind": "stock", "name": "x"}
    with pytest.raises(ValidationError):
        Config.model_validate({"assets": [a, a]})


def test_unknown_key_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"forecst": {}})
