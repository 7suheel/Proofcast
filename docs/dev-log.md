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

## 2026-11-03 — Phase 1: data model and storage

- `models.py`: frozen Pydantic v2 models with `extra="forbid"`. All datetimes must be
  timezone-aware and are normalised to UTC; naive datetimes are rejected.
- Every public data point carries the time it became public: `PriceBar.available_at`,
  `NewsItem.published_at`, `Filing.filed_at` (SEC acceptance time),
  `MacroObservation.published_at` (+ `published_at_estimated`).
- Bar availability is conservative: stock bars count as public at 22:00 UTC on the trading day
  (NYSE closes 20:00/21:00 UTC); crypto daily candles at 00:00 UTC the next day.
- Storage: daily bars as CSV per asset (`data/prices/`), macro as CSV per series
  (`data/macro/`), news and filings as JSON Lines per month. CSV over Parquet: text diffs keep
  the git history readable as an audit trail. Writes are atomic and deterministic.
- News text retention: `prune_news_text` drops `summary` after 90 days, keeping title, url and
  timestamps.
- Macro revisions: a newer fetch may revise a value, but the publication time stays the one first
  stored (or the real FRED first-release time). Known limitation: revised values are used for
  old periods, which is a mild form of lookahead for backtests (documented in methodology).
- Corrections in the ledger are notes only; they never remove a forecast from scoring, so the
  track record cannot be cleaned up after the fact.
- Point-in-time layer `pit.get_data(store, asset, as_of)` filters with strict `<` and then
  re-checks the snapshot with `assert_no_lookahead`. Tests cover exact-boundary cases, session
  hours, crypto midnight, macro publication lag and naive timestamps.
- Tests block all socket connections (`conftest.py`) so no test can touch the network.

## 2026-11-03 — Phase 2: data adapters

Built: shared `HttpClient` (User-Agent, timeouts, retries with backoff that honours
`Retry-After` and never retries faster than the host interval, per-host rate limiter, on-disk
cache with TTL, response size cap, HTTPS only, secrets redacted from errors and cache
metadata); adapters for Coinbase, CoinGecko, Stooq, Yahoo (unofficial), SEC EDGAR submissions
and company facts, FRED (API and CSV), RSS/Atom and GDELT; asset mapping; `proofcast fetch
prices|news|filings|macro|all`.

### Live verification (2026-11-03, from the maintainer's PC, into a scratch directory)

| Adapter | Status |
|---|---|
| Coinbase candles (BTC, SOL since 2024-05) | **verified live**; fixture recorded |
| CoinGecko market_chart | **verified live** (format); fixture recorded |
| Yahoo chart (AAPL, NVDA) | **verified live**; split-adjusted across the NVDA 10:1 split of 2024-06-10; fixture recorded |
| Stooq CSV | **unverified against live API**: the live site returns a JavaScript bot check to scripts. Built from the documented CSV format, tested on a hand-written fixture; the bot page is detected and the chain falls back to Yahoo. |
| FRED fredgraph.csv (no key) | **verified live** for all five series; fixtures recorded |
| FRED API with `FRED_API_KEY` (`output_type=4`) | **unverified against live API** (no key available); hand-written fixture following the documented JSON |
| SEC submissions + company facts | **unverified against live API** for the adapter: SEC rejects User-Agents without an email, and the maintainer's SEC User-Agent is not configured yet. Endpoint reachability was verified in Phase 0. Hand-written fixtures follow the documented JSON. |
| RSS: CNBC (2 feeds), CoinDesk, Cointelegraph, SEC press releases | **verified live**; tests use hand-written feeds so no third-party articles are committed |
| GDELT DOC API | **verified live** (BTC query returned articles); also hit HTTP 429 when queries were close together, which led to the host-interval backoff. Hand-written fixture. |

### Decisions and problems
- Stock prices: config chain `["stooq", "yahoo"]` with `allow_unofficial = true`, because Stooq
  currently blocks scripts. Yahoo is unofficial; set `allow_unofficial = false` to forbid it.
- CoinGecko's public API only serves ~365 days, so long crypto history comes from Coinbase's
  public candles (official API, no key). CoinGecko is the fallback, labelled
  `coingecko:close-only` since it gives one price per day.
- Bars fetched before their session ended are dropped, so a partial day never looks final.
- SEC `acceptanceDateTime` is read as US Eastern Standard Time (UTC-5): never earlier than the
  true publication time, at most one hour late in summer.
- FRED without a key: publication time = end of period + `release_lag_days` at 23:59 UTC,
  flagged as estimated. With a key: real first-release date (`realtime_start`).
- RSS items without a date get the fetch time; future dates are clamped to the fetch time;
  `javascript:` and other non-http links are dropped; HTML is stripped and text capped.
- GDELT: the query adds market words, and an article only counts if its headline names the
  asset (a bare `"Apple"` query returned celebrity and food news in the live test).
- Asset mapping: short names (Meta, Coke, SOL) match only capitalised or upper-case, short
  tickers (KO) only as `$KO`, `(KO)` or `NYSE: KO`; "chase" removed from JPM aliases. Text is
  NFKC-normalised and zero-width characters removed before matching.
- Windows note: paths in `proofcast.toml` must be relative or Windows-style; an MSYS-style
  `/c/...` path is read by Python as `C:\c\...`.

## 2026-11-03 — Phase 3: history and "Why did it fall?"

- Drawdowns: an episode runs from a running peak close to the first close back at that peak;
  the trough is its lowest close. Reported when deeper than the threshold (20% stocks, 35%
  crypto). The last episode may be unrecovered.
- Evidence window: 7 days before the peak until the end of the trough day. Relevance =
  kind weight x closeness x negative-headline bonus x next-day price reaction (formula in the
  `history/linker.py` docstring, to be copied into docs/methodology.md).
- Macro releases only count when they are notable moves (CPI m/m >= 0.4%, unemployment or fed
  funds change >= 0.2 points). A live check on SOL's 2025-26 drawdown showed that routine
  monthly releases otherwise fill the list.
- Market context: asset change vs benchmark change over the same dates (S&P 500 from FRED for
  stocks and BTC; BTC for other crypto); labels "market-wide" (benchmark share >= 50%),
  "partly market-wide" (>= 25%), "mostly asset-specific".
- History agent: prompt in `prompts/history.md`; evidence wrapped in `<untrusted_source>` with
  injection warnings; citations outside the evidence are dropped, contributors without a valid
  citation are dropped, fewer than 3 items forces "thin", and the model is never called when
  there is no evidence. Invalid JSON gets one repair attempt, then the rules summary is used.
- A minimal LLM core (Provider protocol, FakeProvider, token budget, JSON validation) landed
  here because the History agent needs it; real providers come in Phase 4.
- CLI: `proofcast history <asset>`, `proofcast why <asset> <date> [--no-ai]`.

## 2026-11-03 — Phase 4: BYOK LLM layer

- Providers: Anthropic Messages adapter and an OpenAI-compatible adapter (OpenAI, OpenRouter,
  Ollama, any `compat` base URL), written on httpx with no SDK or gateway library.
- `PROOFCAST_MODEL=<provider>:<model>` (everything after the first colon is the model id, so
  `ollama:qwen2.5-coder:7b` works); `--model` overrides it; nothing set means AI off. Each
  provider reads only its own key variable. Keys are never sent over plain http to a
  non-localhost host, and are redacted from every error message.
- Anthropic specifics (checked against the bundled Claude API reference on 2026-11-03):
  `anthropic-version: 2026-10-21`. That API version rejects `temperature`/`top_p`/`top_k` on
  every model with a 400, so the Anthropic adapter does **not** send temperature 0. The
  OpenAI-compatible adapter still does. Current Claude models think before answering: only
  `text` blocks are read, and `max_tokens` has a floor of 8000 because thinking counts toward
  it. `stop_reason` `refusal` and `max_tokens` are errors, so callers use their safe fallback.
  For claude-fable-5-1, claude-opus-5-5, claude-opus-5 and claude-sonnet-5-5 the adapter
  sends `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`), which re-runs a
  declined request on Anthropic's recommended fallback model.
- Suggested Anthropic model in docs: `claude-opus-5-5`.
- Token budget per run (`PROOFCAST_MAX_TOKENS`) is checked before each call (rough prompt
  estimate + max output). Timeout `PROOFCAST_LLM_TIMEOUT`, default 120 s remote, 300 s
  Anthropic, 600 s localhost.
- JSON replies are validated against Pydantic models, with one repair attempt, then the caller's
  fallback.
- `proofcast doctor [--model X] [--live]` reports configuration and key presence, never values.
- Not verified live: no API keys were available in this session, so every adapter is tested
  only with `httpx.MockTransport`.
