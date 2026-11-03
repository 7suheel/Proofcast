# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - unreleased

First public version. Not investment advice.

### Added

- Data adapters for free sources: Coinbase and CoinGecko (crypto), Stooq and the opt-in
  unofficial Yahoo chart API (stocks), SEC EDGAR submissions and company facts, FRED (API or
  CSV), RSS/Atom feeds and the GDELT DOC API. All share a polite HTTP client with retries, rate
  limits, caching, a streaming size cap and secret redaction.
- Point-in-time storage and access: every item carries the time it became public, and agents
  only see what was public before the forecast time.
- "Why did it fall?": drawdown detection, ranked evidence with market context, a rules-based
  summary and an optional History agent.
- Bring-your-own-key LLM layer: Anthropic, OpenAI, OpenRouter, Ollama and any OpenAI-compatible
  server, with a token budget, timeouts, strict JSON validation and `proofcast doctor`.
- Analyst panel (News, Filings, Technical, Macro, History, Bull, Bear, Skeptic, Forecaster), with
  code-enforced citation filtering, shrinkage toward the base rate, clamping and an
  advice-wording filter; `--no-ai` momentum baseline.
- Append-only, hash-chained ledger with `proofcast ledger verify`; resolver from price data only;
  Brier, log score, hit rate and calibration per setup, asset and horizon; baselines scored on
  the same questions; agent weighting from past performance.
- Static website (no JavaScript, strict CSP, dark mode, phone-friendly) and GitHub Actions for
  hourly news, the daily cycle and Pages deployment.
- Security review, threat model, adversarial and lookahead tests, and a release workflow with
  PyPI Trusted Publishing.
