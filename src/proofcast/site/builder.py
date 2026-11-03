"""Build the static site into ``site/`` from committed data and the ledger.

The build never calls an LLM or the network. It verifies the ledger first and shows a warning
banner on every page if verification fails. A copy of the ledger is published next to the
pages so anyone can download it and run ``proofcast ledger verify`` themselves.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from proofcast import DISCLAIMER, __version__
from proofcast.config import Config
from proofcast.history.explain import rules_explanation
from proofcast.history.linker import link_evidence
from proofcast.history.report import asset_drawdowns
from proofcast.ledger import Ledger, LedgerError, VerifyResult
from proofcast.models import Forecast, ForecastResolution
from proofcast.scoring import BASELINE_NAMES, TrackRecord, track_record
from proofcast.site.charts import calibration_chart, price_chart
from proofcast.storage import DataStore, atomic_write_text

MAX_PAST_ROWS = 1000
NEWS_PER_ASSET = 30
FILINGS_PER_ASSET = 20


@dataclass
class SiteBuild:
    out_dir: Path
    pages: list[str] = field(default_factory=list)
    ledger_ok: bool = True


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("proofcast.site", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v, d=0: "-" if v is None else f"{v * 100:.{d}f}%"
    env.filters["num"] = lambda v, spec=".4f": "-" if v is None else format(v, spec)
    return env


def ledger_commits(repo_root: Path, ledger_path: Path) -> dict[int, str]:
    """Line number (1-based) -> commit SHA that added the line, from ``git blame``."""
    try:
        rel = ledger_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return {}
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "blame", "--line-porcelain", "--", rel.as_posix()],  # noqa: S607
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    commits: dict[int, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit() and parts[2].isdigit():
            sha = parts[0]
            if set(sha) != {"0"}:  # all-zero SHA = not committed yet
                commits[int(parts[2])] = sha
    return commits


def _latest(forecasts: list[Forecast]) -> dict[tuple[str, int], Forecast]:
    """The newest forecast per (asset, horizon), preferring AI setups over baselines."""
    best: dict[tuple[str, int], Forecast] = {}
    for f in sorted(forecasts, key=lambda f: (f.created_at, not f.is_baseline)):
        key = (f.asset, f.horizon_days)
        cur = best.get(key)
        if cur is None or f.created_at.date() > cur.created_at.date() or (
            f.created_at.date() == cur.created_at.date() and not f.is_baseline
        ):
            best[key] = f
    return best


def build_site(
    cfg: Config,
    out_dir: Path,
    *,
    now: datetime | None = None,
    repo_root: Path | None = None,
) -> SiteBuild:
    now = now or datetime.now(UTC)
    store = DataStore(cfg.data_dir)
    ledger = Ledger(cfg.ledger_path)
    check: VerifyResult = ledger.verify()
    try:
        entries = ledger.read(verify=False)
    except LedgerError:
        entries = []
    forecasts = [e for e in entries if isinstance(e, Forecast)]
    resolutions = {e.forecast_id: e for e in entries if isinstance(e, ForecastResolution)}
    record: TrackRecord = track_record(forecasts, resolutions, now=now)
    commits = ledger_commits(repo_root or cfg.root, cfg.ledger_path)
    line_of = {e.id: i for i, e in enumerate(entries, start=1)}
    repo = cfg.project.repo_url.rstrip("/")

    def commit_url(entry_id: str) -> str | None:
        sha = commits.get(line_of.get(entry_id, -1))
        return f"{repo}/commit/{sha}" if sha and repo else None

    env = _env()
    common = {
        "project": cfg.project,
        "disclaimer": DISCLAIMER,
        "version": __version__,
        "generated_at": now,
        "ledger_check": check,
        "assets": cfg.assets,
    }
    result = SiteBuild(out_dir=out_dir, ledger_ok=check.ok)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "assets").mkdir(parents=True)

    def render(name: str, path: str, **ctx: object) -> None:
        depth = path.count("/")
        html = env.get_template(name).render(**common, root="../" * depth, page=path, **ctx)
        atomic_write_text(out_dir / path, html)
        result.pages.append(path)

    latest = _latest(forecasts)
    horizons = list(cfg.forecast.horizons)
    rows = []
    for a in cfg.assets:
        cells = [latest.get((a.symbol, h)) for h in horizons]
        rows.append({"asset": a, "cells": cells})
    render("index.html", "index.html", rows=rows, horizons=horizons, record=record)

    why_items = []
    for a in cfg.assets:
        bars = store.read_bars(a.symbol)
        drawdowns = asset_drawdowns(cfg, store, a.symbol)
        news = [n for n in store.read_news() if a.symbol in n.assets][-NEWS_PER_ASSET:][::-1]
        filings = [f for f in store.read_filings() if f.company == a.symbol][-FILINGS_PER_ASSET:][::-1]
        evidence = []
        for d in reversed(drawdowns):
            ev = link_evidence(d, store, cfg, bars=bars)
            evidence.append((ev, rules_explanation(ev)))
        why_items.append({"asset": a, "evidence": evidence})
        render(
            "asset.html",
            f"assets/{a.symbol}.html",
            asset=a,
            chart=price_chart(bars, drawdowns, label=f"{a.symbol} daily close"),
            last_bar=bars[-1] if bars else None,
            forecasts=[latest.get((a.symbol, h)) for h in horizons],
            drawdowns=drawdowns,
            news=news,
            filings=filings,
        )
    render("why.html", "why.html", items=why_items)

    calib = {}
    for s in record.setups:
        calib[s.setup] = s.overall.calibration
    first_ai = next((s for s in record.setups if not s.is_baseline_setup), None)
    if first_ai is not None:
        calib["always 50% (same questions)"] = first_ai.baselines["always_50"].calibration
    past = sorted(forecasts, key=lambda f: f.created_at, reverse=True)[:MAX_PAST_ROWS]
    render(
        "track_record.html",
        "track-record.html",
        record=record,
        baseline_names=BASELINE_NAMES,
        calibration_svg=calibration_chart(calib),
        past=[(f, resolutions.get(f.id), commit_url(f.id)) for f in past],
        total=len(forecasts),
    )
    render("methodology.html", "methodology.html", cfg=cfg)
    render("disclaimer.html", "disclaimer.html")

    static = resources.files("proofcast.site").joinpath("static")
    for item in static.iterdir():
        if item.is_file():
            (out_dir / item.name).write_bytes(item.read_bytes())
    if cfg.ledger_path.exists():
        (out_dir / "ledger").mkdir(exist_ok=True)
        shutil.copyfile(cfg.ledger_path, out_dir / "ledger" / "forecasts.jsonl")
    atomic_write_text(out_dir / "track_record.json", record.model_dump_json(indent=1) + "\n")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return result
