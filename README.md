# Proofcast

**An AI market analyst that shows its own report card.**

> [!WARNING]
> **Not investment advice. Forecasts can be wrong. Past performance does not predict future results.**
> Proofcast is not a trading bot. It never executes trades, never calls an asset "safe" and
> never promises returns. It publishes probabilities, not recommendations.

**Live site: https://7suheel.github.io/Proofcast/** · **Status: early development (0.1.0).** It works end to end, but there is no meaningful track
record yet, so nothing here demonstrates forecasting skill.

## What it does

Proofcast follows a small universe (BTC, ETH, SOL and ten large US stocks) using only free data:
daily prices, news, SEC filings and macro series. Every day it publishes probabilities such as
*"45% chance BTC closes higher in 7 days"*, with the reasoning and sources behind them.

What sets it apart is the **public, tamper-evident track record**:

- Every forecast is appended to a **hash-chained ledger** and committed to git *before* its
  outcome is known. Editing or deleting any entry breaks the chain, and
  `proofcast ledger verify` shows where.
- When the horizon ends, the forecast is **resolved from price data only** by a rule stored in the
  forecast itself.
- The website shows the **Brier score, log score, hit rate and a calibration chart**, next to
  three simple baselines scored on the same questions: always 50%, the historical base rate,
  and momentum. When Proofcast does no better than a baseline, the site says so in plain words.

It also answers **"Why did it fall?"** for every major drawdown of the last 10 years, linking each
fall to the news, filings and notable macro releases around it, together with what the evidence
*can't* tell.

## How a forecast is made

1. **Point-in-time context.** Only data published strictly before the forecast time is used: news
   by publication time, filings by SEC acceptance time, macro by release date, and bars once the
   session has closed.
2. **The panel** (when a model is configured). News, Filings, Technical, Macro and History
   analysts each read their part of the context. Bull and Bear argue both sides, a Skeptic
   reviews them, and a Forecaster gives one probability per horizon. News text is wrapped as
   untrusted data.
3. **Code has the last word.** Citations that are not in the context are dropped. Forecasts are
   pulled toward the base rate when evidence is thin or the agents disagree, then clamped to
   5–95%. Agents with a worse track record get less weight.
4. **No AI?** Everything still works with `--no-ai`. The published forecast is then a clearly
   labelled momentum baseline.

## Screenshots

Screenshots will be added after the first live deploy to GitHub Pages. To see the site now, run
`proofcast site serve` (below). It has no JavaScript, works on phones and supports dark mode.

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/7suheel/proofcast.git
cd proofcast
uv sync
uv run proofcast doctor              # what is configured (never prints keys)
uv run proofcast fetch prices -a BTC -a AAPL
uv run proofcast analyze BTC --no-ai # baseline analysis, no AI
uv run proofcast history AAPL        # major drawdowns
uv run proofcast site serve          # preview the site on http://127.0.0.1:8000
```

### Free local AI with Ollama

```bash
ollama pull qwen2.5:7b
export PROOFCAST_MODEL="ollama:qwen2.5:7b"     # PowerShell: $env:PROOFCAST_MODEL = "ollama:qwen2.5:7b"
uv run proofcast doctor --live
uv run proofcast analyze NVDA
```

### Bring your own key

| Provider | `PROOFCAST_MODEL` | Key variable |
|---|---|---|
| Anthropic | `anthropic:claude-opus-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `openai:<model id>` | `OPENAI_API_KEY` |
| OpenRouter | `openrouter:<model id>` | `OPENROUTER_API_KEY` |
| Any OpenAI-compatible server | `compat:<model id>` + `PROOFCAST_COMPAT_BASE_URL` | `PROOFCAST_COMPAT_API_KEY` (optional) |
| Ollama (local, free) | `ollama:<model>` | none (`OLLAMA_BASE_URL` optional) |

Each provider only ever receives its own key, and keys are never sent over plain HTTP to anything
but localhost. `PROOFCAST_MAX_TOKENS` caps the tokens used per run, and `PROOFCAST_LLM_TIMEOUT`
sets the timeout. A full run asks 9 agents per asset: start with one asset to see the cost.

### Optional data keys

| Variable | Effect without it |
|---|---|
| `PROOFCAST_SEC_USER_AGENT` (e.g. `"Proofcast my-fork admin@example.org"`) | SEC filings are skipped. The SEC requires a contact email in the User-Agent. |
| `FRED_API_KEY` (free) | Macro data comes from the public CSV, with estimated release times. |
| `COINGECKO_DEMO_API_KEY` (free) | The public CoinGecko API is used. |

## Commands

```
proofcast fetch prices|news|filings|macro|all [-a SYMBOL] [--since DATE] [--gdelt]
proofcast history <asset>              proofcast why <asset> <date> [--no-ai]
proofcast analyze <asset> [--no-ai]    proofcast forecast --all [--no-ai] [--dry-run]
proofcast resolve                      proofcast score [--output track_record.json]
proofcast ledger verify                proofcast site build|serve
proofcast doctor [--live]
```

## How the track record works

- `ledger/forecasts.jsonl` holds forecasts, resolutions and corrections, one JSON object per line.
  Each entry contains the SHA-256 hash of the previous one.
- Only **live** forecasts are recorded (made within an hour of now). `analyze --as-of <past date>`
  works as a backtest but prints a warning: LLMs may have memorised what happened, so backtested
  AI accuracy is not real accuracy. Backtests are never written to the ledger.
- Corrections are new entries that refer to an old one. They never remove a forecast from
  scoring.
- GitHub Actions run the daily cycle (fetch → resolve → forecast → score → publish) and commit each
  step separately, so the git history doubles as an audit trail. Each forecast on the site links
  to the commit that recorded it.

Full details: [docs/methodology.md](docs/methodology.md).

## Data sources

All free: Coinbase Exchange and CoinGecko (crypto), Stooq and, opt-in, the unofficial Yahoo
Finance chart API (stocks), SEC EDGAR (filings and reported figures), FRED (macro), public RSS
feeds and the GDELT DOC API (news). Each source's terms and limits are in
[docs/data-sources.md](docs/data-sources.md).

## Limitations

- 7- and 30-day price moves are mostly noise. Calibration matters more than "being right", and it
  takes hundreds of resolved forecasts to tell skill from luck.
- News coverage is incomplete, especially before 2017. Feeds change and disappear.
- Stooq currently blocks scripted downloads, so stock prices fall back to the unofficial Yahoo
  endpoint when `allow_unofficial = true` (the default in `proofcast.toml`).
- Revised macro values are used for past periods, a mild form of lookahead in historical
  analysis. Prices are split-adjusted, not dividend-adjusted.
- Results depend on the model the operator configures, so scores are kept per setup.

## Roadmap

- First live deploy, then let the track record grow (no claims before ~100 resolved forecasts).
- AI-written "Why did it fall?" explanations cached in the repository.
- More horizons and assets via configuration; per-asset calibration pages.
- Reliability diagrams with confidence intervals; a Brier skill score against the base rate.
- See [docs/first-issues.md](docs/first-issues.md) for good first issues.

## Security and conduct

See [SECURITY.md](SECURITY.md), [THREAT_MODEL.md](THREAT_MODEL.md),
[SECURITY_REVIEW.md](SECURITY_REVIEW.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Contributions are welcome: read [CONTRIBUTING.md](CONTRIBUTING.md) and [CLAUDE.md](CLAUDE.md)
first.

## License

Apache-2.0. See [LICENSE](LICENSE).

> **Not investment advice. Forecasts can be wrong. Past performance does not predict future results.**
