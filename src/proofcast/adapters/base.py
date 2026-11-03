"""Helpers shared by the adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from proofcast.models import PriceBar

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time in UTC (adapters accept a clock so tests can freeze time)."""
    return datetime.now(UTC)


def complete_bars(bars: Iterable[PriceBar], fetched_at: datetime) -> list[PriceBar]:
    """Keep only bars whose session had ended when they were fetched.

    A bar fetched during its own trading day is partial; storing it would later look like a
    final close. Dropping it keeps stored bars honest.
    """
    return sorted((b for b in bars if b.available_at <= fetched_at), key=lambda b: b.date)


def env_value(environ: dict[str, str] | None, name: str) -> str | None:
    """Read an environment variable; empty strings count as missing."""
    import os

    source = environ if environ is not None else os.environ
    value = source.get(name, "").strip()
    return value or None
