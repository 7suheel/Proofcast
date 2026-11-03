"""A small, deterministic world for forecasting tests (no network, no real LLM)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from conftest import REPO_ROOT
from helpers import filing, macro, news, series
from proofcast.config import Config, load_config
from proofcast.models import FinancialFact
from proofcast.storage import DataStore

CFG: Config = load_config(REPO_ROOT / "proofcast.toml")
START = date(2023, 1, 1)
DAYS = 500
AS_OF = datetime.combine(START + timedelta(days=DAYS), datetime.min.time(), UTC) + timedelta(
    hours=12
)


def closes(n: int = DAYS) -> list[float]:
    return [100 * (1 + 0.0008 * i) * (1 + 0.03 * math.sin(i / 9)) for i in range(n)]


def build_store(store: DataStore, *, with_text: bool = True) -> DataStore:
    store.write_bars("AAPL", series(closes(), start=START))
    if with_text:
        store.write_news(
            [
                news("Apple unveils new chips", AS_OF - timedelta(days=2)),
                news("Apple supplier warns on demand", AS_OF - timedelta(days=5)),
                news("Apple item published after T", AS_OF + timedelta(minutes=1)),
            ]
        )
        store.write_filings([filing("0000320193-24-000999", AS_OF - timedelta(days=20))])
        store.write_facts(
            "AAPL",
            [
                FinancialFact(
                    asset="AAPL",
                    concept="NetIncomeLoss",
                    unit="USD",
                    value=2.1e10,
                    period_end=date(2024, 3, 30),
                    fiscal_period="Q2",
                    form="10-Q",
                    filed=date(2024, 5, 3),
                    accession="x",
                    published_at=datetime(2024, 5, 4, 4, 59, tzinfo=UTC),
                )
            ],
        )
        store.write_macro(
            "CPIAUCSL",
            [
                macro("CPIAUCSL", date(2024, 1, 1), 309.0, AS_OF - timedelta(days=60)),
                macro("CPIAUCSL", date(2024, 2, 1), 310.0, AS_OF - timedelta(days=30)),
            ],
        )
    return store


ROLE_MARKERS = {
    "the News agent": "news",
    "the Filings agent": "filings",
    "the Technical agent": "technical",
    "the Macro agent": "macro",
    "the History agent": "history",
    "the Bull": "bull",
    "the Bear": "bear",
    "the Skeptic": "skeptic",
    "You are the Forecaster": "forecaster",
}


def role_of(system: str) -> str:
    for marker, role in ROLE_MARKERS.items():
        if marker in system:
            return role
    raise AssertionError("unknown agent role")


def scripted(
    agent_p: float | dict[str, float] = 0.6,
    forecaster_p: float = 0.62,
    *,
    citations: list[str] | None = None,
    overrides: dict[str, str] | None = None,
    seen: list[str] | None = None,
) -> Callable[[str, str], str]:
    """A FakeProvider responder that answers per role."""
    cites = citations if citations is not None else ["N1", "N2", "T:ret_30d", "M1", "F1", "Z99"]

    def respond(system: str, prompt: str) -> str:
        role = role_of(system)
        if seen is not None:
            seen.append(role)
        if overrides and role in overrides:
            return overrides[role]
        if role == "forecaster":
            return json.dumps(
                {
                    "forecasts": [
                        {
                            "horizon_days": h,
                            "probability": forecaster_p,
                            "reasoning": "r",
                            "citations": [*cites, "BOGUS"],
                        }
                        for h in CFG.forecast.horizons
                    ]
                }
            )
        p = agent_p[role] if isinstance(agent_p, dict) else agent_p
        return json.dumps(
            {
                "probabilities": {str(h): p for h in CFG.forecast.horizons},
                "confidence": 0.8,
                "summary": f"{role} view",
                "citations": cites,
            }
        )

    return respond
