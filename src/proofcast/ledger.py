"""The public, append-only, hash-chained ledger (hard rule 5).

``ledger/forecasts.jsonl`` holds one JSON object per line: forecasts, resolutions and
corrections. Each entry stores ``prev_hash`` (the previous entry's hash, or 64 zeros for the
first) and ``hash``::

    hash = sha256(canonical_json(entry without "hash"))

where canonical JSON has sorted keys, no extra whitespace and UTF-8 text. Changing, removing,
reordering or inserting any entry breaks the chain, and ``proofcast ledger verify`` reports
where. Git history adds a second, independent record of when each line was committed.

Nothing in this module edits or deletes lines. Appends refuse to run on a broken chain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from proofcast.models import (
    GENESIS_HASH,
    Correction,
    Forecast,
    ForecastResolution,
    LedgerEntry,
)

Entry = Forecast | ForecastResolution | Correction
_ADAPTER: TypeAdapter[Entry] = TypeAdapter(LedgerEntry)


class LedgerError(RuntimeError):
    """The ledger is broken or an append was refused."""


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(entry: Entry) -> str:
    payload = entry.model_dump(mode="json", exclude={"hash"})
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    errors: list[str] = field(default_factory=list)
    head: str = GENESIS_HASH


def _parse_lines(path: Path) -> tuple[list[Entry], list[str]]:
    entries: list[Entry] = []
    errors: list[str] = []
    if not path.exists():
        return entries, errors
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, start=1):
            if not line.strip():
                errors.append(f"line {n}: blank line")
                continue
            try:
                entries.append(_ADAPTER.validate_json(line))
            except ValidationError as exc:
                errors.append(f"line {n}: not a valid ledger entry ({exc.error_count()} errors)")
    return entries, errors


def verify_entries(entries: Sequence[Entry]) -> VerifyResult:
    errors: list[str] = []
    prev = GENESIS_HASH
    seen: set[str] = set()
    forecast_ids: set[str] = set()
    for i, e in enumerate(entries, start=1):
        if e.prev_hash != prev:
            errors.append(f"entry {i} ({e.id}): prev_hash does not match the previous entry")
        if entry_hash(e) != e.hash:
            errors.append(f"entry {i} ({e.id}): hash does not match its content (edited?)")
        if e.id in seen:
            errors.append(f"entry {i}: duplicate id {e.id}")
        seen.add(e.id)
        if isinstance(e, Forecast):
            forecast_ids.add(e.id)
        elif isinstance(e, ForecastResolution) and e.forecast_id not in forecast_ids:
            errors.append(f"entry {i}: resolution for unknown or later forecast {e.forecast_id}")
        elif isinstance(e, Correction) and e.refers_to not in seen:
            errors.append(f"entry {i}: correction refers to unknown entry {e.refers_to}")
        prev = e.hash
    return VerifyResult(ok=not errors, entries=len(entries), errors=errors, head=prev)


class Ledger:
    """Read, verify and append to the ledger file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, *, verify: bool = True) -> list[Entry]:
        """All entries. By default the whole chain must verify, so nothing downstream (resolve,
        score, the site) ever works from a tampered ledger without saying so."""
        entries, errors = _parse_lines(self.path)
        if not errors and verify:
            errors = verify_entries(entries).errors
        if errors:
            raise LedgerError("; ".join(errors))
        return entries

    def verify(self) -> VerifyResult:
        entries, errors = _parse_lines(self.path)
        result = verify_entries(entries)
        result.errors = errors + result.errors
        result.ok = not result.errors
        return result

    def forecasts(self) -> list[Forecast]:
        return [e for e in self.read() if isinstance(e, Forecast)]

    def resolutions(self) -> dict[str, ForecastResolution]:
        return {e.forecast_id: e for e in self.read() if isinstance(e, ForecastResolution)}

    def append(self, new: Iterable[Entry]) -> list[Entry]:
        """Chain and append entries. Refuses if the existing chain does not verify."""
        new = list(new)
        if not new:
            return []
        check = self.verify()
        if not check.ok:
            raise LedgerError("refusing to append to a broken ledger: " + "; ".join(check.errors))
        existing_ids = {e.id for e in self.read()}
        prev = check.head
        chained: list[Entry] = []
        for e in new:
            if e.id in existing_ids:
                raise LedgerError(f"duplicate id {e.id}")
            existing_ids.add(e.id)
            linked = e.model_copy(update={"prev_hash": prev, "hash": ""})
            linked = linked.model_copy(update={"hash": entry_hash(linked)})
            chained.append(linked)
            prev = linked.hash
        result = verify_entries([*self.read(), *chained])
        if not result.ok:
            raise LedgerError("; ".join(result.errors))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            for e in chained:
                fh.write(e.model_dump_json() + "\n")
        return chained
