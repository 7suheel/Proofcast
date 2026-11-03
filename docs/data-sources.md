# Data sources

Every source is free. Proofcast sends a descriptive User-Agent
(`proofcast/<version> (+https://github.com/7suheel/proofcast)`), caches responses, rate-limits
per host and retries politely. Check each provider's current terms before running a public
deployment: terms change, and the operator of a deployment is responsible for following them.

Status as of 2026-11-03 (see `docs/dev-log.md` for the live checks).

| Source | Used for | Access | Limits Proofcast applies | Terms and notes | Verified live |
|---|---|---|---|---|---|
| **Coinbase Exchange** public market data (`api.exchange.coinbase.com/products/{id}/candles`) | Crypto daily bars, long history (BTC since 2015) | No key | 300 candles per request; daily job only | Public market-data endpoint of Coinbase's Exchange API; subject to Coinbase's API terms and rate limits | Yes |
| **CoinGecko** public API (`api.coingecko.com/api/v3`) | Crypto daily closes (fallback, ~365 days) | No key, or free demo key `COINGECKO_DEMO_API_KEY` | One request per asset per run | CoinGecko API terms; attribution required for public display ("Data provided by CoinGecko"); public/demo plans limit history to 365 days | Yes |
| **Stooq** CSV (`stooq.com/q/d/l/`) | US stock daily bars (default) | No key | One request per asset per run | Free downloads for personal use; as of 2026-11 it serves a JavaScript bot check to scripts, so it currently fails and Proofcast falls back | **No** (blocked); fixture only |
| **Yahoo Finance chart API** (`query1.finance.yahoo.com/v8/finance/chart`) | US stock daily bars (fallback) | No key | One request per asset per run; only when `allow_unofficial = true` | **Unofficial and undocumented**; may break or be restricted at any time; Yahoo's terms apply. Set `allow_unofficial = false` to forbid it | Yes |
| **SEC EDGAR** (`data.sec.gov/submissions`, `data.sec.gov/api/xbrl/companyfacts`) | Filings (8-K, 10-Q, 10-K) and headline reported figures | No key; **User-Agent with a contact email required** (`PROOFCAST_SEC_USER_AGENT`) | ≤ 9 requests per second, cached for 1–6 hours | SEC fair-access policy: identify yourself, at most 10 requests per second. Filings are skipped without the User-Agent | Endpoints reachable; adapter not run live (no User-Agent configured) |
| **FRED** API (`api.stlouisfed.org/fred/series/observations`) | Macro series with real first-release dates | Free key `FRED_API_KEY` | One request per series per run | FRED API terms of use; attribution to FRED, Federal Reserve Bank of St. Louis. Some series have their own copyright notes | No (no key); fixture only |
| **FRED** graph CSV (`fred.stlouisfed.org/graph/fredgraph.csv`) | Macro series without a key | No key | One request per series per run | Public download link; same FRED terms; no release dates, so they are estimated | Yes |
| **RSS feeds** (CNBC Top News, CNBC Finance, CoinDesk, Cointelegraph, SEC press releases) | Recent headlines | No key | Hourly; 10-minute cache; ≤ 500 items per feed; summaries pruned after 90 days | Each publisher's terms apply. Proofcast stores only titles, links, times and short summaries, and links back to the source. Reuters' public RSS was discontinued and is not used | Yes |
| **GDELT DOC 2.0 API** (`api.gdeltproject.org/api/v2/doc/doc`) | Historical news search for drawdowns (opt-in `--gdelt`) | No key | ≥ 5.5 s between requests; cached 30 days for old windows | GDELT asks for at most one request every 5 seconds; open data with attribution to the GDELT Project; coverage starts around 2017 | Yes (and saw its 429 when spaced too closely) |

## What is stored in the repository

- `data/prices/*.csv`: daily bars only.
- `data/news/YYYY-MM.jsonl`: title, URL, source, times, asset tags. The summary is pruned after 90
  days.
- `data/filings/YYYY-MM.jsonl`, `data/fundamentals/*.jsonl`: SEC metadata and a few reported
  figures.
- `data/macro/*.csv`: macro observations with publication times.
- `ledger/forecasts.jsonl`: the public ledger.

No API keys, cookies or personal data are stored.
