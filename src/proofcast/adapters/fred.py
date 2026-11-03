"""FRED macro data.

* With ``FRED_API_KEY``: the official API with ``output_type=4`` ("initial release only"), whose
  ``realtime_start`` is the date each value was first published. We stamp it at 23:59 UTC of
  that day, after every US release time.
* Without a key: the public ``fredgraph.csv`` download. It has no release dates, so
  ``published_at`` is a conservative estimate: end of the observation period plus the series'
  configured ``release_lag_days``, at 23:59 UTC, flagged ``published_at_estimated``.
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from proofcast.adapters.base import Clock, env_value, utcnow
from proofcast.config import MacroSeries
from proofcast.models import MacroObservation
from proofcast.net.http import HttpClient, SourceBlockedError

FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_KEY_ENV = "FRED_API_KEY"
END_OF_DAY = time(23, 59)


def period_end(day: date, frequency: str) -> date:
    """Last day of the observation period that starts on ``day``."""
    if frequency == "monthly":
        last = calendar.monthrange(day.year, day.month)[1]
        return day.replace(day=last)
    return day


def estimated_publication(day: date, series: MacroSeries) -> datetime:
    end = period_end(day, series.frequency)
    return datetime.combine(end + timedelta(days=series.release_lag_days), END_OF_DAY, UTC)


def _value(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if raw in {"", "."}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_fred_csv(series: MacroSeries, text: str, fetched_at: datetime) -> list[MacroObservation]:
    stripped = text.lstrip()
    if stripped.startswith("<"):
        raise SourceBlockedError("fred: got HTML instead of CSV")
    reader = csv.reader(io.StringIO(stripped))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise ValueError(f"fred: unexpected CSV for {series.series_id}")
    out = []
    for row in reader:
        if len(row) < 2:
            continue
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError:
            continue
        value = _value(row[1])
        if value is None:
            continue
        out.append(
            MacroObservation(
                series_id=series.series_id,
                date=day,
                value=value,
                published_at=estimated_publication(day, series),
                published_at_estimated=True,
                source="fred-csv",
                fetched_at=fetched_at,
            )
        )
    return out


def parse_fred_api(
    series: MacroSeries, payload: Any, fetched_at: datetime
) -> list[MacroObservation]:
    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        raise ValueError(f"fred: unexpected API payload for {series.series_id}")
    out = []
    for o in payload["observations"]:
        try:
            day = date.fromisoformat(o["date"])
            released = date.fromisoformat(o["realtime_start"])
        except (KeyError, ValueError, TypeError):
            continue
        value = _value(o.get("value"))
        if value is None:
            continue
        # A first-release date before the period ended is impossible; fall back to the estimate.
        published = datetime.combine(released, END_OF_DAY, UTC)
        estimated = False
        if released < period_end(day, series.frequency):
            published = estimated_publication(day, series)
            estimated = True
        out.append(
            MacroObservation(
                series_id=series.series_id,
                date=day,
                value=value,
                published_at=published,
                published_at_estimated=estimated,
                source="fred-api",
                fetched_at=fetched_at,
            )
        )
    return out


def fetch_macro(
    client: HttpClient,
    series: MacroSeries,
    *,
    since: date | None = None,
    environ: dict[str, str] | None = None,
    clock: Clock = utcnow,
) -> list[MacroObservation]:
    now = clock()
    key = env_value(environ, API_KEY_ENV)
    if key:
        params: dict[str, str | int | float] = {
            "series_id": series.series_id,
            "api_key": key,
            "file_type": "json",
            "output_type": 4,
            "realtime_start": "1776-07-04",
            "realtime_end": "9999-12-31",
        }
        if since:
            params["observation_start"] = since.isoformat()
        return parse_fred_api(series, client.get_json(FRED_API_URL, params=params), now)
    csv_params: dict[str, str | int | float] = {"id": series.series_id}
    if since:
        csv_params["cosd"] = since.isoformat()
    return parse_fred_csv(series, client.get_text(FRED_CSV_URL, params=csv_params), now)
