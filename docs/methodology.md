# Methodology

> Not investment advice. Forecasts can be wrong. Past performance does not predict future results.

## 1. The question

For each asset and horizon *h* (default 7 and 30 calendar days), a forecast made at time *T*
asks:

> Will the close of the first daily bar dated on or after `reference_date + h` be **strictly
> higher** than the reference close?

The **reference bar** is the last daily bar that was complete before *T*. The rule is stored with
the forecast (`resolution_rule`): asset, reference date and close, target date,
`first_bar_on_or_after`, comparison `>`, and `max_wait_days` (10).

## 2. When data becomes public (no lookahead)

| Data | `published_at` / `available_at` |
|---|---|
| US stock daily bar | 22:00 UTC on the trading day (later than the 20:00 or 21:00 UTC close) |
| Crypto daily bar (UTC day) | 00:00 UTC on the next day |
| News (RSS) | the item's published/updated time, or the fetch time if it has none; future times are clamped to the fetch time |
| News (GDELT) | `seendate` |
| SEC filing | EDGAR acceptance time, read as US Eastern **Standard** Time (never earlier than the truth) |
| SEC reported figure | end of the filing day, US Eastern |
| FRED with key | first-release date (`realtime_start`, `output_type=4`) at 23:59 UTC |
| FRED without key | end of period + `release_lag_days` at 23:59 UTC, flagged as estimated |

`pit.get_data(store, asset, T)` returns only items with time strictly before *T*. The agent
context is checked again with `assert_no_lookahead`. Forecasts are written to the ledger only if
*T* is within one hour of the real time.

## 3. Baselines

- **Always 50%.**
- **Base rate**: over the bars known before *T* (last 10 years), the share of days whose close was
  below the close of the first bar on or after `day + h`, counting only outcomes already known at
  *T*. Smoothed: `(k + 1) / (n + 2)`.
- **Momentum**: `clamp(base_rate + 0.10 * tanh(r / σ), 0.05, 0.95)`, where `r` is the log return
  over the last *h* days, and `σ = sd(daily log returns, last 252 bars) * sqrt(bars in the
  window - 1)`.

All three are stored on every forecast (`baselines`).

## 4. The AI panel

Context ids: `N#` news (last 30 days, ≤ 25), `F#` filings (last 180 days, ≤ 10), `K#` reported
figures, `M#` macro, `T:<name>` technical indicators, `B:<h>` base rates, `H#` history.
News and filings are wrapped in `<untrusted_source>`; computed numbers are `<data>`.

Order: News, Filings, Technical, Macro and History analysts, then Bull, Bear and Skeptic, then the
Forecaster. An analyst with no items of its kind is skipped.

Post-processing by code:

1. **Citations.** Ids not in the context are dropped. Confidence is multiplied by
   `1 - 0.5 * dropped / cited`, and halved again if no valid citation remains.
2. **Panel average.** Weighted mean of agent probabilities with weight
   `track_record_weight * confidence`; *disagreement* is the weighted standard deviation.
3. **Shrinkage.** `p = base + s * (p_forecaster - base)`, where `s = 1`, halved if fewer than 3
   distinct context items (base rates excluded) were validly cited, and halved again if the
   disagreement exceeds 0.15. If the forecaster's reply is invalid, the panel average is used;
   if nothing is usable, the base rate is.
4. **Clamp** to [0.05, 0.95].
5. **Advice wording** ("buy", "sell", "guaranteed", "safe investment") is removed from model text.

**Agent weights.** Over each agent's last 50 resolved forecasts, with Brier score *B*:
`w = clamp(exp(-10 * (B - 0.25)), 0.25, 2)` once it has at least 10, otherwise 1.

## 5. Resolution

The target bar is the first stored bar dated on or after the target date that was complete at
resolution time. The outcome compares the target close with the reference close **from the
current stored series**, so split adjustments cannot flip it; the recorded reference close stays
in the ledger, with a note when the two differ by more than 1%. If no bar exists within
`max_wait_days`, the forecast is `void` and excluded from scores.

## 6. Scores

For resolved forecasts with probability *p* and outcome *o* ∈ {0, 1}:

- Brier `mean((p - o)²)`, lower is better; always-50% scores 0.25.
- Log score `mean(ln(p if o else 1 - p))`, higher is better.
- Hit rate: share on the right side of 50%; exactly 50% counts as half.
- Calibration: ten buckets [0, 0.1) … [0.9, 1.0], with mean forecast and observed frequency.

Scores are computed per setup (`ai:<provider>:<model>` or `baseline:momentum`), per asset and per
horizon. The three baselines are scored on the **same questions** as each setup. The verdict says
"NOT better than …" whenever any baseline's Brier score is as good or better, and warns when there
are fewer than 30 resolved forecasts.

## 7. The ledger

One JSON object per line: forecasts, resolutions and corrections.
`hash = sha256(canonical_json(entry without "hash"))`, with sorted keys, compact separators and
UTF-8; `prev_hash` is the previous entry's hash (64 zeros for the first). Reads and appends verify
the whole chain. Corrections never edit or remove an entry.

## 8. "Why did it fall?"

- Drawdown: from a running peak close to the first close back at that peak. The trough is the
  lowest close in between. Reported when deeper than 20% (stocks) or 35% (crypto).
- Evidence window: 7 days before the peak until the end of the trough day.
- Relevance: `kind_weight * closeness * (1 + 0.5 * negative_words) * (1 + reaction)`.
  - kind weight: 8-K item 2.02 1.0, other 8-K 0.8, 10-Q/10-K 0.7, news 0.6, macro 0.4–0.45;
  - closeness: 1 inside the fall, `exp(-days_before_start / 3)` before it;
  - reaction: the next-day fall in units of the typical daily move, capped at 2.
  Macro releases count only if notable (CPI month-on-month change ≥ 0.4%, unemployment or fed
  funds change ≥ 0.2 points).
- Market context: the asset's change against the benchmark (S&P 500 for stocks and BTC; BTC for
  other crypto) over the same dates. "Market-wide" when the benchmark fell at least half as much;
  "partly" from a quarter; otherwise "mostly asset-specific".
- The AI History agent may only cite linked evidence, is never called without evidence, and must
  say when evidence is thin.

## 9. Known limitations

See the README. In short: noisy target, incomplete news history, macro revisions, no dividend
adjustment, model-dependent results, and LLM memorisation, which makes AI backtests meaningless
as evidence of skill.
