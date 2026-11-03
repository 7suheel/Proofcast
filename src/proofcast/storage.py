"""On-disk storage for committed data.

Layout under the data directory::

    prices/<SYMBOL>.csv        daily bars, one file per asset, sorted by date
    macro/<SERIES_ID>.csv      macro observations, one file per series
    news/<YYYY-MM>.jsonl       news items, by month of publication
    filings/<YYYY-MM>.jsonl    SEC filings, by month of acceptance

Everything is plain text so that the git history doubles as an audit trail. Writes are atomic
(write to a temporary file, then rename) and deterministic (stable ordering), so re-running a
fetch with the same data produces no diff.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from proofcast.models import Filing, MacroObservation, NewsItem, PriceBar

NEWS_TEXT_RETENTION_DAYS = 90

_BAR_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "fetched_at",
    "available_at",
]
_MACRO_COLUMNS = [
    "date",
    "value",
    "published_at",
    "published_at_estimated",
    "source",
    "fetched_at",
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _month_key(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m")


def _safe_name(name: str) -> str:
    """Reject names that could escape the data directory."""
    if not name or any(c in name for c in "/\\:") or name.startswith("."):
        raise ValueError(f"unsafe file name: {name!r}")
    return name


class DataStore:
    """Read and write committed data under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- prices ------------------------------------------------------------------

    def prices_path(self, symbol: str) -> Path:
        return self.root / "prices" / f"{_safe_name(symbol)}.csv"

    def read_bars(self, symbol: str) -> list[PriceBar]:
        """All stored bars for ``symbol``, sorted by date. Empty if none."""
        path = self.prices_path(symbol)
        if not path.exists():
            return []
        df = pl.read_csv(path, schema_overrides={c: pl.Utf8 for c in _BAR_COLUMNS})
        bars = [
            PriceBar.model_validate(
                {
                    "asset": symbol,
                    "date": row["date"],
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "source": row["source"],
                    "fetched_at": row["fetched_at"],
                    "available_at": row["available_at"],
                }
            )
            for row in df.iter_rows(named=True)
        ]
        return sorted(bars, key=lambda b: b.date)

    def write_bars(self, symbol: str, bars: Iterable[PriceBar]) -> int:
        """Merge ``bars`` into the stored file (newer fetch wins per date).

        Returns the number of dates that were added or changed.
        """
        existing = {b.date: b for b in self.read_bars(symbol)}
        changed = 0
        for bar in bars:
            if bar.asset != symbol:
                raise ValueError(f"bar for {bar.asset} written to {symbol}")
            old = existing.get(bar.date)
            if old is None or _bar_values(old) != _bar_values(bar):
                existing[bar.date] = bar
                changed += 1
        if not changed:
            return 0
        ordered = [existing[d] for d in sorted(existing)]
        df = pl.DataFrame(
            {
                "date": [b.date.isoformat() for b in ordered],
                "open": [_num(b.open) for b in ordered],
                "high": [_num(b.high) for b in ordered],
                "low": [_num(b.low) for b in ordered],
                "close": [_num(b.close) for b in ordered],
                "volume": [_num(b.volume) for b in ordered],
                "source": [b.source for b in ordered],
                "fetched_at": [_iso(b.fetched_at) for b in ordered],
                "available_at": [_iso(b.available_at) for b in ordered],
            },
            schema={c: pl.Utf8 for c in _BAR_COLUMNS},
        )
        atomic_write_text(self.prices_path(symbol), df.write_csv())
        return changed

    # -- macro -------------------------------------------------------------------

    def macro_path(self, series_id: str) -> Path:
        return self.root / "macro" / f"{_safe_name(series_id)}.csv"

    def read_macro(self, series_id: str) -> list[MacroObservation]:
        path = self.macro_path(series_id)
        if not path.exists():
            return []
        df = pl.read_csv(path, schema_overrides={c: pl.Utf8 for c in _MACRO_COLUMNS})
        obs = [
            MacroObservation.model_validate(
                {
                    "series_id": series_id,
                    "date": row["date"],
                    "value": float(row["value"]),
                    "published_at": row["published_at"],
                    "published_at_estimated": row["published_at_estimated"] == "true",
                    "source": row["source"],
                    "fetched_at": row["fetched_at"],
                }
            )
            for row in df.iter_rows(named=True)
        ]
        return sorted(obs, key=lambda o: o.date)

    def write_macro(self, series_id: str, observations: Iterable[MacroObservation]) -> int:
        """Merge observations; the value may be revised by a newer fetch.

        A real first-release time (from FRED vintage data) is never replaced by an estimate, and
        an observation keeps the publication time it was first stored with otherwise.
        """
        existing = {o.date: o for o in self.read_macro(series_id)}
        changed = 0
        for o in observations:
            if o.series_id != series_id:
                raise ValueError(f"observation for {o.series_id} written to {series_id}")
            old = existing.get(o.date)
            merged = o
            if old is not None and not (
                old.published_at_estimated and not o.published_at_estimated
            ):
                merged = o.model_copy(
                    update={
                        "published_at": old.published_at,
                        "published_at_estimated": old.published_at_estimated,
                    }
                )
            if old is None or _macro_values(old) != _macro_values(merged):
                existing[o.date] = merged
                changed += 1
        if not changed:
            return 0
        ordered = [existing[d] for d in sorted(existing)]
        df = pl.DataFrame(
            {
                "date": [o.date.isoformat() for o in ordered],
                "value": [_num(o.value) for o in ordered],
                "published_at": [_iso(o.published_at) for o in ordered],
                "published_at_estimated": [
                    "true" if o.published_at_estimated else "false" for o in ordered
                ],
                "source": [o.source for o in ordered],
                "fetched_at": [_iso(o.fetched_at) for o in ordered],
            },
            schema={c: pl.Utf8 for c in _MACRO_COLUMNS},
        )
        atomic_write_text(self.macro_path(series_id), df.write_csv())
        return changed

    # -- news ----------------------------------------------------------------------

    def _jsonl_files(self, kind: str) -> list[Path]:
        folder = self.root / kind
        if not folder.exists():
            return []
        return sorted(folder.glob("*.jsonl"))

    def read_news(self) -> list[NewsItem]:
        items = [NewsItem.model_validate(r) for r in self._iter_jsonl("news")]
        return sorted(items, key=lambda n: (n.published_at, n.id))

    def write_news(self, items: Iterable[NewsItem]) -> int:
        """Add news items, deduplicated by id. Asset tags of duplicates are merged.

        Returns the number of new or changed items.
        """
        return self._merge_jsonl("news", items, _merge_news)

    def prune_news_text(self, now: datetime, days: int = NEWS_TEXT_RETENTION_DAYS) -> int:
        """Drop the raw summary text of news older than ``days``; keep title, url, timestamps."""
        cutoff = now - timedelta(days=days)
        pruned = 0
        for path in self._jsonl_files("news"):
            rows = [NewsItem.model_validate(r) for r in _read_jsonl(path)]
            new_rows = []
            for item in rows:
                if item.summary is not None and item.published_at < cutoff:
                    item = item.model_copy(update={"summary": None})
                    pruned += 1
                new_rows.append(item)
            if pruned:
                _write_jsonl(path, new_rows)
        return pruned

    # -- filings ---------------------------------------------------------------------

    def read_filings(self) -> list[Filing]:
        items = [Filing.model_validate(r) for r in self._iter_jsonl("filings")]
        return sorted(items, key=lambda f: (f.filed_at, f.id))

    def write_filings(self, filings: Iterable[Filing]) -> int:
        return self._merge_jsonl("filings", filings, _keep_first)

    # -- helpers -----------------------------------------------------------------------

    def _iter_jsonl(self, kind: str) -> Iterator[dict[str, object]]:
        for path in self._jsonl_files(kind):
            yield from _read_jsonl(path)

    def _merge_jsonl(
        self,
        kind: str,
        items: Iterable[NewsItem] | Iterable[Filing],
        merge: Callable[[Any, Any], Any],
    ) -> int:
        model: type[NewsItem] | type[Filing] = NewsItem if kind == "news" else Filing
        by_month: dict[str, list[NewsItem | Filing]] = {}
        for item in items:
            by_month.setdefault(_month_key(_sort_key(item)[0]), []).append(item)
        changed = 0
        for month, new_items in sorted(by_month.items()):
            path = self.root / kind / f"{month}.jsonl"
            current: dict[str, NewsItem | Filing] = {}
            if path.exists():
                for row in _read_jsonl(path):
                    parsed = model.model_validate(row)
                    current[parsed.id] = parsed
            month_changed = 0
            for item in new_items:
                old = current.get(item.id)
                merged = item if old is None else merge(old, item)
                if old is None or merged != old:
                    current[item.id] = merged
                    month_changed += 1
            if month_changed:
                _write_jsonl(path, sorted(current.values(), key=_sort_key))
                changed += month_changed
        return changed


def _sort_key(item: NewsItem | Filing) -> tuple[datetime, str]:
    if isinstance(item, NewsItem):
        return (item.published_at, item.id)
    return (item.filed_at, item.id)


def _merge_news(old: NewsItem, new: NewsItem) -> NewsItem:
    """Keep the earliest publication time and union the asset tags."""
    assets = sorted(set(old.assets) | set(new.assets))
    summary = old.summary if old.summary is not None else new.summary
    return old.model_copy(update={"assets": assets, "summary": summary})


def _keep_first(old: Filing, new: Filing) -> Filing:
    return old


def _read_jsonl(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected an object")
            yield row


def _write_jsonl(path: Path, items: Iterable[NewsItem | Filing]) -> None:
    lines = [item.model_dump_json(exclude_none=False) for item in items]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _num(value: float) -> str:
    """Format a number compactly and losslessly."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _bar_values(bar: PriceBar) -> tuple[float, float, float, float, float, str]:
    return (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.source)


def _macro_values(o: MacroObservation) -> tuple[float, datetime, bool, str]:
    return (o.value, o.published_at, o.published_at_estimated, o.source)


def parse_date(value: str) -> date:
    """Parse ``YYYY-MM-DD``."""
    return date.fromisoformat(value)
