# Development log

Dated notes: decisions, problems, what is verified and what is not.

## 2026-11-03 — Phase 0: project setup

### Environment
- Local development on Windows 11 with uv 0.11.24 and Python 3.12.13 (installed by uv).
  git 2.55 was already installed; GitHub CLI 2.103.3 installed with winget.
- Name check: `proofcast` is free on PyPI (`https://pypi.org/pypi/proofcast/json` → 404), so the
  package name is `proofcast`.

### Data host reachability (from the maintainer's PC, 2026-11-03)
Checked with `curl` (HEAD first, then a small GET for anything unclear).

| Host / endpoint | Result | Notes |
|---|---|---|
| api.coingecko.com `/api/v3/ping`, `/coins/bitcoin/market_chart` | reachable (200) | Public API returns JSON without a key. Public/demo tiers only serve ~365 days of history. |
| api.exchange.coinbase.com `/products/BTC-USD/candles` | reachable (200) | Public market data, no key, 300 candles per request, BTC history back to 2015. Used for long crypto history. |
| www.sec.gov | reachable (200) | `www.sec.gov/files/company_tickers.json` returned 403 for a User-Agent without an email address. |
| data.sec.gov `/submissions/CIK##########.json`, `/api/xbrl/companyfacts/...` | reachable (200) | Works with a descriptive User-Agent; SEC asks for a contact email in it, and rejected a UA without one later in the session. The SEC adapter therefore requires `PROOFCAST_SEC_USER_AGENT` with an email. |
| api.stlouisfed.org (FRED API) | reachable, 400 without key | "Variable api_key is not set". Needs `FRED_API_KEY`. |
| fred.stlouisfed.org `/graph/fredgraph.csv?id=...` | reachable (200) | Public CSV download without key (FEDFUNDS, SP500 tested). Used as keyless fallback. |
| api.gdeltproject.org `/api/v2/doc/doc` | reachable, rate limited | HEAD timed out; GET returned the "limit requests to one every 5 seconds" message. Adapter waits ≥5.5 s between calls. |
| stooq.com `/q/d/l/?s=aapl.us&i=d` | **blocked for scripts** | HTTP 200 but the body is a JavaScript browser-verification page, not CSV. Adapter is built from the documented CSV format and tested against fixtures; it detects the HTML challenge and falls back. Unverified against live API. |
| query1.finance.yahoo.com `/v8/finance/chart/AAPL` | reachable (200) | Unofficial, undocumented endpoint. Used only behind `allow_unofficial`. |
| www.cnbc.com RSS | reachable (GET 200; HEAD 404) | |
| www.coindesk.com `/arc/outboundfeeds/rss/` | reachable (308 → 200) | |
| cointelegraph.com `/rss` | reachable (200) | |
| www.sec.gov `/news/pressreleases.rss` | reachable (200) | |
| feeds.reuters.com | **unreachable** | Reuters discontinued its public RSS feeds; not used. |

Note: the very first reachability check this session sent a User-Agent containing the
maintainer's email address to these hosts. Later checks and all project code use a project
User-Agent; the SEC one is configured by the maintainer.

### Decisions
- Build backend: hatchling. Lint/format: ruff; types: mypy strict with the pydantic plugin.
- Dataframe library: **polars** (rule 7: pick one). Price bars are stored as CSV, which diffs
  well in git and keeps the history auditable.
- `.gitattributes` forces LF line endings so hashes, formatting and diffs are identical on
  Windows and Linux.
- GitHub Actions pinned to commit SHAs (resolved with `git ls-remote`): checkout v7.0.1,
  setup-uv v10.3.3.
