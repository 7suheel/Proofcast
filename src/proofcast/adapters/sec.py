"""SEC EDGAR adapter: company submissions (filings list) and company facts (XBRL).

SEC fair-access rules: at most 10 requests per second and a User-Agent that identifies the
requester with a contact email (https://www.sec.gov/os/accessing-edgar-data). The User-Agent
comes from ``PROOFCAST_SEC_USER_AGENT``, e.g. ``"Proofcast example admin@example.org"``. Without
it the adapter refuses to run (and the CLI skips filings with a warning).

``acceptanceDateTime`` is the time a filing became public on EDGAR. The submissions JSON marks
it with ``Z`` but EDGAR operates on US Eastern time; to be safe against lookahead we interpret
the clock time as Eastern Standard Time (UTC-5), which is never earlier than the true time.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from proofcast.adapters.base import Clock, env_value, utcnow
from proofcast.models import Asset, Filing, FinancialFact
from proofcast.net.http import HttpClient

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{document}"
SEC_HOSTS = ("data.sec.gov", "www.sec.gov")
SEC_MIN_INTERVAL = 0.11  # seconds -> at most ~9 requests per second
USER_AGENT_ENV = "PROOFCAST_SEC_USER_AGENT"

DEFAULT_FORMS = frozenset({"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "6-K", "20-F"})
EASTERN_STANDARD_OFFSET = timedelta(hours=5)

FACT_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
    "OperatingIncomeLoss",
)

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


class SecUserAgentError(RuntimeError):
    """``PROOFCAST_SEC_USER_AGENT`` is missing or has no contact email."""


def sec_user_agent(environ: dict[str, str] | None = None) -> str:
    """Return the configured SEC User-Agent or raise :class:`SecUserAgentError`."""
    ua = env_value(environ, USER_AGENT_ENV)
    if not ua or not _EMAIL.search(ua):
        raise SecUserAgentError(
            f"{USER_AGENT_ENV} must be set to a descriptive User-Agent with a contact email, "
            'for example "Proofcast my-fork admin@example.org"'
        )
    return ua


def configure_rate_limit(client: HttpClient) -> None:
    for host in SEC_HOSTS:
        client.rate_limiter.set_interval(host, SEC_MIN_INTERVAL)


def parse_acceptance(value: str) -> datetime:
    """Parse EDGAR ``acceptanceDateTime`` conservatively (see module docstring)."""
    clean = value.strip().rstrip("Z")
    clean = clean.split(".")[0]
    naive = datetime.fromisoformat(clean)
    return (naive + EASTERN_STANDARD_OFFSET).replace(tzinfo=UTC)


def parse_submissions(
    asset: Asset,
    payload: Any,
    fetched_at: datetime,
    *,
    forms: frozenset[str] = DEFAULT_FORMS,
    since: date | None = None,
) -> list[Filing]:
    """Parse ``filings.recent`` from the submissions JSON (column-oriented arrays)."""
    try:
        recent = payload["filings"]["recent"]
        accessions = recent["accessionNumber"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"sec: unexpected submissions payload for {asset.symbol}") from exc
    cik = str(payload.get("cik", asset.cik or "")).lstrip("0") or "0"

    def col(name: str, i: int) -> str:
        values = recent.get(name) or []
        return str(values[i]) if i < len(values) and values[i] is not None else ""

    filings = []
    for i, accession in enumerate(accessions):
        form = col("form", i)
        if form not in forms:
            continue
        filing_date = col("filingDate", i)
        if since and filing_date and date.fromisoformat(filing_date) < since:
            continue
        accepted = col("acceptanceDateTime", i)
        if accepted:
            filed_at = parse_acceptance(accepted)
        elif filing_date:
            # No acceptance time: assume the end of the filing day (Eastern), conservative.
            filed_at = datetime.combine(date.fromisoformat(filing_date), time(23, 59), UTC)
            filed_at += EASTERN_STANDARD_OFFSET
        else:
            continue
        document = col("primaryDocument", i)
        url = (
            ARCHIVE_URL.format(cik_int=cik, accession=accession.replace("-", ""), document=document)
            if document
            else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        )
        items = [x.strip() for x in col("items", i).split(",") if x.strip()]
        description = col("primaryDocDescription", i) or None
        filings.append(
            Filing(
                id=str(accession),
                company=asset.symbol,
                cik=asset.cik or cik.zfill(10),
                form_type=form,
                filed_at=filed_at,
                url=url,
                items=items,
                title=description,
                fetched_at=fetched_at,
            )
        )
    return filings


def fetch_filings(
    client: HttpClient,
    asset: Asset,
    *,
    since: date | None = None,
    environ: dict[str, str] | None = None,
    clock: Clock = utcnow,
) -> list[Filing]:
    """Recent filings (EDGAR keeps ~1000 most recent in ``filings.recent``)."""
    if not asset.cik:
        return []
    ua = sec_user_agent(environ)
    configure_rate_limit(client)
    payload = client.get_json(SUBMISSIONS_URL.format(cik=asset.cik), user_agent=ua, cache_ttl=3600)
    return parse_submissions(asset, payload, clock(), since=since)


def parse_company_facts(
    asset: Asset, payload: Any, *, concepts: tuple[str, ...] = FACT_CONCEPTS, since: date | None
) -> list[FinancialFact]:
    """Extract a few headline US-GAAP facts with their filing dates."""
    try:
        gaap = payload["facts"]["us-gaap"]
    except (KeyError, TypeError):
        return []
    out: dict[tuple[str, str, date, str], FinancialFact] = {}
    for concept in concepts:
        units = (gaap.get(concept) or {}).get("units") or {}
        for unit, values in units.items():
            for v in values:
                try:
                    filed = date.fromisoformat(v["filed"])
                    period_end = date.fromisoformat(v["end"])
                    value = float(v["val"])
                    form = str(v.get("form", ""))
                    accession = str(v.get("accn", ""))
                except (KeyError, ValueError, TypeError):
                    continue
                if since and filed < since:
                    continue
                published = datetime.combine(filed, time(23, 59), UTC) + EASTERN_STANDARD_OFFSET
                fact = FinancialFact(
                    asset=asset.symbol,
                    concept=concept,
                    unit=unit,
                    value=value,
                    period_end=period_end,
                    fiscal_period=v.get("fp"),
                    form=form,
                    filed=filed,
                    accession=accession,
                    published_at=published,
                )
                key = (concept, unit, period_end, form)
                # Keep the first time a value was reported (later filings repeat old periods).
                if key not in out or out[key].filed > filed:
                    out[key] = fact
    return sorted(out.values(), key=lambda f: (f.filed, f.concept, f.period_end))


def fetch_company_facts(
    client: HttpClient,
    asset: Asset,
    *,
    since: date | None = None,
    environ: dict[str, str] | None = None,
) -> list[FinancialFact]:
    if not asset.cik:
        return []
    ua = sec_user_agent(environ)
    configure_rate_limit(client)
    payload = client.get_json(
        COMPANY_FACTS_URL.format(cik=asset.cik), user_agent=ua, cache_ttl=6 * 3600
    )
    return parse_company_facts(asset, payload, since=since)
