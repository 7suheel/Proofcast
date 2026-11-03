"""Resolver, scoring, baselines, agent weighting and the ledger/resolve/score CLI."""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import REPO_ROOT
from helpers import bar, series
from proofcast.cli import app
from proofcast.ledger import Ledger
from proofcast.models import AgentOpinion, Forecast, ForecastResolution, ResolutionRule
from proofcast.resolve import resolve_due, resolve_one
from proofcast.scoring import (
    brier,
    calibration,
    hit_rate,
    log_score,
    summarize,
    track_record,
    track_record_text,
    verdict,
)
from proofcast.storage import DataStore
from proofcast.weighting import MIN_RESOLVED, agent_brier_scores, agent_weights, weight_for

REF = date(2024, 1, 1)


def fc(
    i: int,
    p: float,
    *,
    setup: str = "ai:fake:m",
    asset: str = "AAPL",
    horizon: int = 7,
    ref_close: float = 100,
    baselines: dict[str, float] | None = None,
    opinions: list[AgentOpinion] | None = None,
) -> Forecast:
    rule = ResolutionRule(
        asset=asset,
        reference_date=REF,
        reference_close=ref_close,
        target_date=REF + timedelta(days=horizon),
    )
    return Forecast(
        id=f"f{i}-{setup}-{asset}-{horizon}",
        created_at=datetime(2024, 1, 2, tzinfo=UTC) + timedelta(seconds=i),
        asset=asset,
        question="q",
        horizon_days=horizon,
        probability=p,
        base_rate=0.55,
        baselines=baselines or {"always_50": 0.5, "base_rate": 0.55, "momentum": 0.6},
        resolution_rule=rule,
        setup=setup,
        is_baseline=setup.startswith("baseline"),
        agent_opinions=opinions or [],
    )


def resolution(f: Forecast, outcome: bool | None) -> ForecastResolution:
    return ForecastResolution(
        id=f"res-{f.id}",
        forecast_id=f.id,
        resolved_at=datetime(2024, 2, 1, tzinfo=UTC),
        status="void" if outcome is None else "resolved",
        outcome=outcome,
        target_bar_date=None if outcome is None else REF + timedelta(days=7),
        target_close=None if outcome is None else 101.0,
        reference_close=100,
        price_source="t",
    )


# -- resolver ---------------------------------------------------------------------------


def test_resolves_from_first_bar_on_or_after_target() -> None:
    f = fc(1, 0.6)
    bars = [bar(REF, 100), bar(REF + timedelta(days=6), 90), bar(REF + timedelta(days=9), 105)]
    r = resolve_one(f, bars, datetime(2024, 1, 20, tzinfo=UTC))
    assert r is not None and r.status == "resolved"
    assert r.outcome is True
    assert r.target_bar_date == REF + timedelta(days=9)
    assert r.id == f"res-{f.id}"


def test_not_higher_when_equal() -> None:
    f = fc(1, 0.6)
    r = resolve_one(
        f, [bar(REF, 100), bar(REF + timedelta(days=7), 100)], datetime(2024, 1, 20, tzinfo=UTC)
    )
    assert r is not None and r.outcome is False


def test_waits_for_target_bar_to_complete() -> None:
    f = fc(1, 0.6)
    bars = [bar(REF, 100), bar(REF + timedelta(days=7), 105)]
    during = datetime.combine(REF + timedelta(days=7), datetime.min.time(), UTC) + timedelta(
        hours=18
    )
    assert resolve_one(f, bars, during) is None
    assert resolve_one(f, bars, during + timedelta(hours=5)) is not None


def test_void_after_max_wait() -> None:
    f = fc(1, 0.6)
    assert resolve_one(f, [bar(REF, 100)], datetime(2024, 1, 12, tzinfo=UTC)) is None
    r = resolve_one(f, [bar(REF, 100)], datetime(2024, 1, 25, tzinfo=UTC))
    assert r is not None and r.status == "void" and r.outcome is None


def test_split_adjusted_series_does_not_flip_outcome() -> None:
    # Recorded reference close 1000; the series was later split-adjusted 10:1.
    f = fc(1, 0.6, ref_close=1000)
    bars = [bar(REF, 100), bar(REF + timedelta(days=7), 105)]
    r = resolve_one(f, bars, datetime(2024, 1, 20, tzinfo=UTC))
    assert r is not None and r.outcome is True
    assert "adjusted" in r.note
    assert r.reference_close == 1000


def test_resolve_due_skips_resolved_and_future(store: DataStore) -> None:
    store.write_bars("AAPL", series([100 + i for i in range(40)], start=REF))
    a, b, c = fc(1, 0.6), fc(2, 0.6, horizon=30), fc(3, 0.6, horizon=365)
    now = datetime(2024, 2, 5, tzinfo=UTC)
    out = resolve_due([a, b, c], {a.id}, store, now)
    assert [r.forecast_id for r in out] == [b.id]


# -- scores -----------------------------------------------------------------------------


def test_scoring_formulas() -> None:
    pairs = [(0.8, True), (0.3, False), (0.6, False), (0.5, True)]
    assert brier(pairs) == pytest.approx((0.04 + 0.09 + 0.36 + 0.25) / 4)
    assert log_score(pairs) == pytest.approx(
        (math.log(0.8) + math.log(0.7) + math.log(0.4) + math.log(0.5)) / 4
    )
    assert hit_rate(pairs) == pytest.approx((1 + 1 + 0 + 0.5) / 4)
    assert brier([]) is None and log_score([]) is None and hit_rate([]) is None
    assert log_score([(0.0, True)]) == pytest.approx(math.log(1e-6))


def test_always_50_scores_quarter() -> None:
    assert brier([(0.5, True), (0.5, False)]) == 0.25


def test_calibration_buckets() -> None:
    buckets = calibration([(0.05, False), (0.15, True), (0.95, True), (1.0, True), (0.1, False)])
    assert len(buckets) == 10
    assert buckets[0].n == 1
    assert buckets[1].n == 2 and buckets[1].observed_frequency == 0.5
    assert buckets[9].n == 2 and buckets[9].observed_frequency == 1.0
    assert buckets[5].mean_probability is None


def test_summary_ignores_void_and_unresolved() -> None:
    fs = [fc(1, 0.7), fc(2, 0.7), fc(3, 0.7)]
    res = {fs[0].id: resolution(fs[0], True), fs[1].id: resolution(fs[1], None)}
    s = summarize("g", "ai:fake:m", fs, res)
    assert s.n_forecasts == 3 and s.n_resolved == 1
    assert s.brier == pytest.approx(0.09)


def test_baselines_scored_on_same_questions() -> None:
    fs = [fc(i, 0.9) for i in range(40)]
    res = {f.id: resolution(f, i % 2 == 0) for i, f in enumerate(fs)}
    rec = track_record(fs, res, now=datetime(2024, 3, 1, tzinfo=UTC))
    (setup,) = rec.setups
    assert setup.baselines["always_50"].brier == 0.25
    assert setup.baselines["always_50"].n_resolved == setup.overall.n_resolved == 40
    assert setup.overall.brier == pytest.approx(0.41)
    assert "NOT better" in setup.verdict and "always 50" in setup.verdict


def test_verdict_texts() -> None:
    fs = [fc(i, 0.8) for i in range(5)]
    res = {f.id: resolution(f, True) for f in fs}
    rec = track_record(fs, res, now=datetime(2024, 3, 1, tzinfo=UTC))
    v = rec.setups[0].verdict
    assert "too few" in v and "Better than all three" in v
    empty = track_record([fc(1, 0.6)], {}, now=datetime(2024, 3, 1, tzinfo=UTC))
    assert "No resolved forecasts" in empty.setups[0].verdict
    assert verdict(empty.setups[0].overall, {}).startswith("No resolved")


def test_track_record_groups_and_text() -> None:
    fs = [
        fc(1, 0.6),
        fc(2, 0.6, asset="MSFT"),
        fc(3, 0.6, horizon=30),
        fc(4, 0.55, setup="baseline:momentum"),
    ]
    res = {f.id: resolution(f, True) for f in fs}
    res[fs[2].id] = resolution(fs[2], None)
    rec = track_record(fs, res, now=datetime(2024, 3, 1, tzinfo=UTC))
    assert rec.n_forecasts == 4 and rec.n_resolved == 3 and rec.n_void == 1
    ai = next(s for s in rec.setups if s.setup == "ai:fake:m")
    assert {s.asset for s in ai.by_asset} == {"AAPL", "MSFT"}
    assert {s.horizon_days for s in ai.by_horizon} == {7, 30}
    base = next(s for s in rec.setups if s.setup == "baseline:momentum")
    assert base.is_baseline_setup
    text = track_record_text(rec)
    assert "vs always_50" in text and "Not investment advice" in text


# -- weighting -------------------------------------------------------------------------


def test_weight_formula() -> None:
    assert weight_for(0.10, MIN_RESOLVED - 1) == 1.0
    assert weight_for(0.25, 20) == 1.0
    assert weight_for(0.30, 20) == pytest.approx(math.exp(-0.5), abs=1e-4)
    assert weight_for(0.20, 20) == pytest.approx(math.exp(0.5), abs=1e-4)
    assert weight_for(0.9, 20) == 0.25
    assert weight_for(0.0, 20) == 2.0


def test_agent_weights_from_ledger_history() -> None:
    fs = []
    for i in range(20):
        ops = [
            AgentOpinion(agent="good", probabilities={7: 0.8}, confidence=1),
            AgentOpinion(agent="bad", probabilities={7: 0.2}, confidence=1),
        ]
        fs.append(fc(i, 0.6, opinions=ops))
    res = {f.id: resolution(f, True) for f in fs}
    scores = agent_brier_scores(fs, res)
    assert scores["good"] == (pytest.approx(0.04), 20)
    weights = agent_weights(fs, res)
    assert weights["good"] > 1 > weights["bad"]
    assert agent_brier_scores(fs, res, window=5)["good"][1] == 5


# -- CLI ---------------------------------------------------------------------------------


def test_cli_resolve_score_verify(tmp_path: Path) -> None:
    (tmp_path / "proofcast.toml").write_text(
        (REPO_ROOT / "proofcast.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    store = DataStore(tmp_path / "data")
    store.write_bars("AAPL", series([100 + i for i in range(40)], start=REF))
    ledger = Ledger(tmp_path / "ledger" / "forecasts.jsonl")
    ledger.append([fc(1, 0.7), fc(2, 0.4, horizon=30), fc(3, 0.6, horizon=3650)])
    cfg = ["--config", str(tmp_path / "proofcast.toml")]
    runner = CliRunner()
    r = runner.invoke(app, ["resolve", "--dry-run", *cfg])
    assert r.exit_code == 0 and "not written" in r.output
    r = runner.invoke(app, ["resolve", *cfg])
    assert "recorded 2 resolution(s)" in r.output
    assert runner.invoke(app, ["resolve", *cfg]).output.strip().endswith("recorded 0 resolution(s)")
    out = tmp_path / "track_record.json"
    r = runner.invoke(app, ["score", "--output", str(out), *cfg])
    assert r.exit_code == 0, r.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["n_resolved"] == 2
    r = runner.invoke(app, ["ledger", "verify", *cfg])
    assert r.exit_code == 0 and "ok: 5 entries" in r.output
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["probability"] = 0.99
    lines[0] = json.dumps(tampered)
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = runner.invoke(app, ["ledger", "verify", *cfg])
    assert r.exit_code == 1 and "FAILED" in r.output
    assert runner.invoke(app, ["resolve", *cfg]).exit_code == 1
    assert runner.invoke(app, ["score", *cfg]).exit_code == 1
