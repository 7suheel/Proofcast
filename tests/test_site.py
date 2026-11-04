"""Static site generation from fixtures (no network, no AI)."""

import json
import re
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from helpers import news, series
from proofcast import DISCLAIMER
from proofcast.cli import app
from proofcast.config import load_config
from proofcast.forecast.engine import make_forecasts
from proofcast.ledger import Ledger
from proofcast.resolve import resolve_due
from proofcast.site.builder import build_site, ledger_commits
from proofcast.site.charts import axis_label, calibration_chart, price_chart
from proofcast.storage import DataStore

START = date(2023, 1, 1)
EVIL = '<script>alert("x")</script> Apple shares <img src=x onerror=alert(1)> plunge'


def _project(tmp_path: Path) -> Path:
    (tmp_path / "proofcast.toml").write_text(
        (REPO_ROOT / "proofcast.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    store = DataStore(tmp_path / "data")
    closes = [100 + 30 * ((i // 60) % 2) - (i % 60) * 0.6 for i in range(500)]
    store.write_bars("AAPL", series(closes, start=START))
    store.write_bars(
        "BTC", series([30000 + 50 * i for i in range(500)], start=START, asset="BTC", kind="crypto")
    )
    when = datetime.combine(START + timedelta(days=75), datetime.min.time(), UTC)
    store.write_news([news(EVIL, when, url="https://example.com/evil")])
    cfg = load_config(tmp_path / "proofcast.toml")
    as_of = datetime.combine(START + timedelta(days=480), datetime.min.time(), UTC)
    ledger = Ledger(cfg.ledger_path)
    for sym in ("AAPL", "BTC"):
        ledger.append(make_forecasts(cfg, store, sym, as_of, None).forecasts)
    ledger.append(resolve_due(ledger.forecasts(), set(), store, as_of + timedelta(days=40)))
    return tmp_path


def test_build_all_pages(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cfg = load_config(root / "proofcast.toml")
    out = root / "site"
    result = build_site(cfg, out, now=datetime(2024, 6, 1, tzinfo=UTC))
    assert result.ledger_ok
    expected = {
        "index.html",
        "why.html",
        "track-record.html",
        "methodology.html",
        "disclaimer.html",
        "assets/AAPL.html",
        "assets/BTC.html",
    }
    assert expected <= set(result.pages)
    assert (out / "style.css").exists()
    assert (out / ".nojekyll").exists()
    assert (out / "ledger" / "forecasts.jsonl").read_bytes() == cfg.ledger_path.read_bytes()
    record = json.loads((out / "track_record.json").read_text(encoding="utf-8"))
    assert record["n_resolved"] == 2  # the 7-day ones; 30-day targets are past the data
    for page in result.pages:
        html = (out / page).read_text(encoding="utf-8")
        assert DISCLAIMER in html, page
        assert "Content-Security-Policy" in html
        assert "<script" not in html.lower(), page
        assert "https://cdn" not in html
        assert not re.search(r"\b(buy|sell)\b", html, re.IGNORECASE), page


def test_untrusted_text_is_escaped(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cfg = load_config(root / "proofcast.toml")
    build_site(cfg, root / "site", now=datetime(2024, 6, 1, tzinfo=UTC))
    html = (root / "site" / "assets" / "AAPL.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert 'rel="noopener nofollow ugc noreferrer"' in html


def test_pages_show_forecasts_and_baselines(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cfg = load_config(root / "proofcast.toml")
    build_site(cfg, root / "site", now=datetime(2024, 6, 1, tzinfo=UTC))
    index = (root / "site" / "index.html").read_text(encoding="utf-8")
    assert "baseline, no AI" in index and "base rate" in index
    track = (root / "site" / "track-record.html").read_text(encoding="utf-8")
    assert "Baseline: always 50" in track and "Calibration" in track
    assert "too few to tell" in track
    assert "<svg" in track
    why = (root / "site" / "why.html").read_text(encoding="utf-8")
    assert "What we can&#39;t tell" in why or "What we can't tell" in why


def test_broken_ledger_banner(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cfg = load_config(root / "proofcast.toml")
    lines = cfg.ledger_path.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[0])
    data["probability"] = 0.95
    lines[0] = json.dumps(data)
    cfg.ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = build_site(cfg, root / "site", now=datetime(2024, 6, 1, tzinfo=UTC))
    assert not result.ledger_ok
    html = (root / "site" / "index.html").read_text(encoding="utf-8")
    assert "Ledger verification FAILED" in html


def test_empty_project_builds(tmp_path: Path) -> None:
    (tmp_path / "proofcast.toml").write_text(
        (REPO_ROOT / "proofcast.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    cfg = load_config(tmp_path / "proofcast.toml")
    result = build_site(cfg, tmp_path / "site", now=datetime(2024, 6, 1, tzinfo=UTC))
    assert "index.html" in result.pages
    assert "No forecasts recorded yet" in (tmp_path / "site" / "track-record.html").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commit_links_from_git_blame(tmp_path: Path) -> None:
    root = _project(tmp_path)
    git = [
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.org",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "ledger"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "ledger"], cwd=root, check=True)
    cfg = load_config(root / "proofcast.toml")
    commits = ledger_commits(root, cfg.ledger_path)
    assert len(commits) == len(cfg.ledger_path.read_text(encoding="utf-8").splitlines())
    build_site(cfg, root / "site", now=datetime(2024, 6, 1, tzinfo=UTC), repo_root=root)
    track = (root / "site" / "track-record.html").read_text(encoding="utf-8")
    assert "github.com/7suheel/proofcast/commit/" in track
    assert ledger_commits(root / "nope", cfg.ledger_path) == {}


def test_charts() -> None:
    assert "Not enough" in price_chart([])
    svg = price_chart(series([1, 2, 3, 2], start=START), label="a<b")
    assert svg.startswith("<svg") and "a&lt;b" in svg
    assert axis_label(125_000) == "125k" and axis_label(2_500_000) == "2.5M"
    assert axis_label(1234) == "1,234" and axis_label(0.5123) == "0.512"
    assert "<svg" in calibration_chart({})


def test_cli_site_build(tmp_path: Path) -> None:
    root = _project(tmp_path)
    r = CliRunner().invoke(
        app, ["site", "build", "--out", str(root / "out"), "--config", str(root / "proofcast.toml")]
    )
    assert r.exit_code == 0, r.output
    assert (root / "out" / "index.html").exists()


def test_workflows_are_pinned_and_least_privilege() -> None:
    workflows = list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert {w.name for w in workflows} >= {"ci.yml", "news.yml", "daily.yml", "pages.yml"}
    for w in workflows:
        text = w.read_text(encoding="utf-8")
        assert "permissions: {}" in text or "permissions:\n  contents: read" in text, w.name
        for ref in re.findall(r"uses:\s*([^\s#]+)", text):
            if ref.startswith("./"):
                continue
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{w.name}: {ref} is not pinned to a SHA"
        assert "${{ github.event" not in text, f"{w.name}: untrusted event data interpolated"
    daily = (REPO_ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    assert "group: proofcast-data" in daily
    assert "vars.PROOFCAST_MODEL != ''" in daily


def test_log_scale_for_long_histories() -> None:
    grown = price_chart(series([1.0 + i for i in range(100)], start=START))
    flat = price_chart(series([100.0 + i % 5 for i in range(100)], start=START))
    assert "log scale" in grown
    assert "log scale" not in flat


def test_filing_display_title_skips_repeated_form() -> None:
    from helpers import filing

    f = filing("x-1", datetime(2024, 1, 1, tzinfo=UTC), form_type="10-K")
    assert f.model_copy(update={"title": "10-K"}).display_title is None
    assert f.model_copy(update={"title": " 10-k "}).display_title is None
    assert f.model_copy(update={"title": "Annual report"}).display_title == "Annual report"
    assert f.display_title is None
