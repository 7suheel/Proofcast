# Proofcast

**An AI market analyst that shows its own report card.**

> [!WARNING]
> **Not investment advice. Forecasts can be wrong. Past performance does not predict future results.**
> Proofcast is not a trading bot. It never executes trades, never calls an asset "safe" and
> never promises returns. It publishes probabilities, not recommendations.

**Status: early development.** Nothing here is ready for use yet.

## What it is

Proofcast tracks real news, company filings, macro data and price history for a small universe of
stocks and crypto assets. Several AI agents look at each asset from different angles (news,
filings, technicals, macro, history, bull, bear, skeptic), and a forecaster turns their views into
probabilities such as *"58% chance BTC closes higher in 30 days"*, with reasoning and sources.

Its main feature is a **public, tamper-evident track record**:

- every forecast is timestamped and committed to a hash-chained, append-only ledger *before* the
  outcome is known;
- each forecast is scored automatically from price data when its horizon ends;
- the website shows the calibration chart, the Brier score and a comparison with simple
  baselines (always 50%, historical base rate, momentum), and says so plainly when Proofcast is
  not better than them.

It also answers **"Why did X fall?"** for every major drawdown in the last 10 years, by linking
each fall to the news, filings and macro releases around it.

## Principles

- **Zero cost**: free data sources, static site on GitHub Pages, jobs on GitHub Actions.
- **Bring your own key** (Anthropic, OpenAI-compatible, OpenRouter) or run a free local model with
  Ollama. Everything except the AI agents also works with `--no-ai`.
- **No lookahead**: an agent forecasting at time T only sees data published before T.
- **Untrusted input stays data**: news and filings are wrapped and never treated as instructions.

## License

Apache-2.0. See [LICENSE](LICENSE).
