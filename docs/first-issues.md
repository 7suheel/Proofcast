# Good first issues

Each of these is self-contained, has a clear "done" and needs no API key. Read
[CLAUDE.md](../CLAUDE.md) first: every change ships with tests, and tests never touch the network.

1. **Brier skill score.** Add `BSS = 1 - Brier / Brier(base rate)` to `ScoreSummary` and the track
   record page. *Done:* formula documented in `docs/methodology.md`, unit tests with hand-computed
   values, shown on the site.

2. **Per-asset calibration on asset pages.** Show a small calibration chart and the Brier score for
   that asset's resolved forecasts on `assets/<SYMBOL>.html`. *Done:* template, test in
   `tests/test_site.py`, phone layout still has no horizontal scroll.

3. **CSV export of the track record.** `proofcast score --csv path` writes one row per resolved
   forecast (id, asset, horizon, probability, baselines, outcome). *Done:* CLI option, test, link
   on the track record page.

4. **More RSS feeds via config only.** Find two more free, reliable finance or crypto feeds whose
   terms allow headline use, add them to `proofcast.toml`, and add hand-written fixture tests that
   mirror their structure. *Done:* entries in `docs/data-sources.md` with terms.

5. **Asset aliases audit.** Improve `aliases` in `proofcast.toml` to reduce false positives and
   negatives (e.g. "Alphabet" vs "Google", product names). *Done:* new parametrised cases in
   `tests/test_news_adapters.py::test_asset_matching`.

6. **`proofcast ledger show <id>`.** Print one ledger entry with its resolution and the commit
   that recorded it. *Done:* CLI command, test with a temporary git repo.

7. **Accessibility pass on the site.** Check headings, contrast in light and dark mode, table
   captions and SVG titles. *Done:* fixes plus a test that every page has exactly one `<h1>` and
   every chart has an `aria-label`.

8. **Homoglyph normalisation for tickers.** Map common Cyrillic/Greek look-alikes to Latin before
   asset matching (finding F10 in `SECURITY_REVIEW.md`). *Done:* small mapping table, tests with
   `АAPL` (Cyrillic A) and similar.
