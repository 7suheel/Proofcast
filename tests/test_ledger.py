"""The append-only, hash-chained ledger and tampering detection (rule 5)."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from proofcast.ledger import Ledger, LedgerError, entry_hash, verify_entries
from proofcast.models import (
    GENESIS_HASH,
    Correction,
    Forecast,
    ForecastResolution,
    ResolutionRule,
)

T = datetime(2024, 1, 2, 23, tzinfo=UTC)


def fc(i: int, p: float = 0.6) -> Forecast:
    rule = ResolutionRule(
        asset="BTC",
        reference_date=date(2024, 1, 1),
        reference_close=100,
        target_date=date(2024, 1, 8),
    )
    return Forecast(
        id=f"f{i}",
        created_at=T + timedelta(minutes=i),
        asset="BTC",
        question="q",
        horizon_days=7,
        probability=p,
        base_rate=0.52,
        baselines={"always_50": 0.5, "base_rate": 0.52, "momentum": 0.55},
        resolution_rule=rule,
        setup="baseline:momentum",
        is_baseline=True,
    )


def res(fid: str) -> ForecastResolution:
    return ForecastResolution(
        id=f"r-{fid}",
        forecast_id=fid,
        resolved_at=T + timedelta(days=8),
        status="resolved",
        outcome=True,
        target_bar_date=date(2024, 1, 8),
        target_close=110,
        reference_close=100,
        price_source="test",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger" / "forecasts.jsonl")


def _lines(ledger: Ledger) -> list[str]:
    return ledger.path.read_text(encoding="utf-8").splitlines()


def _write(ledger: Ledger, lines: list[str]) -> None:
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_append_builds_chain(ledger: Ledger) -> None:
    out = ledger.append([fc(1), fc(2)])
    assert out[0].prev_hash == GENESIS_HASH
    assert out[1].prev_hash == out[0].hash
    assert all(e.hash == entry_hash(e) for e in out)
    ledger.append([res("f1")])
    result = ledger.verify()
    assert result.ok and result.entries == 3
    assert [e.id for e in ledger.read()] == ["f1", "f2", "r-f1"]
    assert set(ledger.resolutions()) == {"f1"}


def test_append_empty_is_noop(ledger: Ledger) -> None:
    assert ledger.append([]) == []
    assert not ledger.path.exists()
    assert ledger.verify().ok


def test_tampered_probability_detected(ledger: Ledger) -> None:
    ledger.append([fc(1), fc(2), fc(3)])
    lines = _lines(ledger)
    data = json.loads(lines[1])
    data["probability"] = 0.99
    lines[1] = json.dumps(data)
    _write(ledger, lines)
    result = ledger.verify()
    assert not result.ok
    assert any("entry 2" in e and "hash does not match" in e for e in result.errors)
    # Normal reads refuse a tampered ledger, so nothing downstream scores it.
    with pytest.raises(LedgerError):
        ledger.forecasts()
    assert len(ledger.read(verify=False)) == 3


def test_rehashed_edit_still_breaks_next_link(ledger: Ledger) -> None:
    ledger.append([fc(1), fc(2), fc(3)])
    entries = ledger.read()
    forged = entries[1].model_copy(update={"probability": 0.9})
    forged = forged.model_copy(update={"hash": entry_hash(forged)})
    _write(
        ledger,
        [entries[0].model_dump_json(), forged.model_dump_json(), entries[2].model_dump_json()],
    )
    result = ledger.verify()
    assert not result.ok
    assert any("entry 3" in e and "prev_hash" in e for e in result.errors)


def test_deleted_reordered_inserted_lines_detected(ledger: Ledger) -> None:
    ledger.append([fc(1), fc(2), fc(3)])
    lines = _lines(ledger)
    _write(ledger, [lines[0], lines[2]])
    assert not ledger.verify().ok
    _write(ledger, [lines[1], lines[0], lines[2]])
    assert not ledger.verify().ok
    _write(ledger, [lines[0], lines[1], lines[1], lines[2]])
    assert not ledger.verify().ok


def test_garbage_and_blank_lines_detected(ledger: Ledger) -> None:
    ledger.append([fc(1)])
    _write(ledger, [*_lines(ledger), "", '{"kind": "forecast"}'])
    result = ledger.verify()
    assert not result.ok
    assert any("blank" in e for e in result.errors)
    assert any("not a valid ledger entry" in e for e in result.errors)
    with pytest.raises(LedgerError):
        ledger.read()


def test_append_refused_on_broken_chain(ledger: Ledger) -> None:
    ledger.append([fc(1), fc(2)])
    lines = _lines(ledger)
    _write(ledger, [lines[1]])
    with pytest.raises(LedgerError, match="broken"):
        ledger.append([fc(3)])


def test_duplicates_refused(ledger: Ledger) -> None:
    ledger.append([fc(1)])
    with pytest.raises(LedgerError, match="duplicate"):
        ledger.append([fc(1)])
    with pytest.raises(LedgerError, match="duplicate"):
        ledger.append([fc(2), fc(2)])
    assert ledger.verify().entries == 1


def test_resolution_and_correction_references() -> None:
    bad = verify_entries([res("nope")])
    assert not bad.ok
    assert any("unknown or later forecast" in e for e in bad.errors)


def test_correction_is_a_new_entry(ledger: Ledger) -> None:
    ledger.append([fc(1)])
    c = Correction(
        id="c1", created_at=T + timedelta(hours=1), refers_to="f1", reason="source url typo"
    )
    ledger.append([c])
    assert ledger.verify().ok
    assert ledger.read()[0].probability == 0.6  # type: ignore[union-attr]
    with pytest.raises(LedgerError):
        ledger.append([Correction(id="c2", created_at=T, refers_to="missing", reason="x")])


def test_file_is_append_only_lf(ledger: Ledger) -> None:
    ledger.append([fc(1)])
    first = ledger.path.read_bytes()
    ledger.append([fc(2)])
    after = ledger.path.read_bytes()
    assert after.startswith(first)
    assert b"\r\n" not in after
