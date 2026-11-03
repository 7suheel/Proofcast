# Test fixtures

- `recorded/` — small responses recorded from the live sources on 2026-11-03 (numbers only, no
  third-party text): Coinbase candles, CoinGecko market chart, Yahoo chart, FRED graph CSV.
- `handwritten/` — files written by hand to follow each provider's documented format, used where
  the live source was blocked or needs credentials we do not have (Stooq, SEC EDGAR, FRED API,
  GDELT), and for RSS/Atom feeds (so we do not commit third-party articles). Headlines in these
  files are invented.
