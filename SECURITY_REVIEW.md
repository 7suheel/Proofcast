# Security review — 2026-11-03

Scope: the whole repository at the end of Phase 8 (package, prompts, site, workflows, scripts).
Method: manual review against [THREAT_MODEL.md](THREAT_MODEL.md), adversarial and lookahead
tests (`tests/test_adversarial.py`), `pip-audit`, `zizmor` and `actionlint`.

## Tool results

| Tool | Version | Result |
|---|---|---|
| `pip-audit` (on `uv export` of the locked dependencies) | latest via `uvx` | **No known vulnerabilities** |
| `zizmor --offline .github/workflows` | latest via `uvx` | Before: 2 medium (`artipacked`), 1 low/help. After fixes: 0 medium, 0 high; 1 help-level note (below) |
| `actionlint` | 1.7.12 (`actionlint-py`) | Before: 2 YAML errors. After: clean |
| `pytest` | — | 297 tests pass, including injection, lookahead, secret and pathological-input tests |

## Findings

Severity: **High** = could falsify the track record or leak a key with little effort;
**Medium** = realistic weakness with limited impact or a pre-condition; **Low** = defence in
depth.

| ID | Severity | Finding | Status | Test |
|---|---|---|---|---|
| F1 | Medium | `news.yml` and `daily.yml` checkouts persisted the push token in `.git/config` (zizmor `artipacked`). | **Fixed**: `persist-credentials: false`; `scripts/commit-data.sh` passes the token per command as a masked header; verified no credentials remain in `.git/config`. | zizmor clean; manual run against a local bare repo |
| F2 | Medium | `resolve` and `score` read the ledger without verifying the hash chain, so a tampered ledger would have been scored and published. | **Fixed** (Phase 6): `Ledger.read()` verifies the whole chain by default; the site shows a red banner if verification fails. | `test_ledger.py::test_tampered_probability_detected`, `test_scoring.py::test_cli_resolve_score_verify`, `test_site.py::test_broken_ledger_banner` |
| F3 | Medium | A GDELT or RSS item with a non-string field (e.g. `"url": 5`) crashed the parser: a hostile or broken source could stop the news job. | **Fixed**: `_safe_url` and `clean_text` ignore non-strings. | `test_adversarial.py::test_gdelt_garbage_payloads` |
| F4 | Medium | Model output could contain investment-advice wording ("buy", "sell", "guaranteed"), against hard rule 3. | **Fixed**: `security.neutralize_advice` removes it from agent summaries, forecaster reasoning and History explanations, with a note. | `test_adversarial.py::test_neutralize_advice`, `test_model_advice_wording_never_published` |
| F5 | Medium | `scripts/commit-data.sh` failed when `ledger/` did not exist yet, so the very first scheduled run would have failed. | **Fixed**: only existing paths are added. | Manual run against a local bare repo |
| F6 | Medium | `.gitignore` entry `site/` also matched `src/proofcast/site/`: the site package was missing from a commit and skipped by ruff. | **Fixed**: `/site/` (root only); `git status --ignored` checked. | CI lint now covers the package |
| F7 | Low | The 20 MB response cap was checked after the whole body was downloaded. | **Fixed**: streaming read stops as soon as the cap is exceeded. | `test_adversarial.py::test_response_cap_stops_streaming` |
| F8 | Low | JSON-shaped injection detection missed keys like `"forecasts"`. | **Fixed**: pattern matches any of our output keys followed by `:`. | `test_adversarial.py::test_fake_json_in_news_is_just_data` |
| F9 | Low | Two workflow `run:` values were invalid YAML (unquoted `": "`). | **Fixed** (Phase 7). | actionlint clean |
| F10 | Low | Homoglyphs from other scripts (e.g. Cyrillic "А") are not mapped to Latin by NFKC, so `АAPL` is not tagged as AAPL. | **Accepted**: it only reduces tagging; tagged text is still wrapped and flagged. | — |
| F11 | Low | The LLM transport buffers replies up to 5 MB before checking size. | **Accepted**: the endpoint is the operator's own provider. | — |
| F12 | Info | zizmor `self-repository` suggests a newer syntax for calling a local reusable workflow. | **Accepted**: `./.github/workflows/pages.yml` is the documented form; no security impact. | — |

No high-severity findings were found. Every high and medium finding is fixed and covered by a
test or a recorded manual check.

## Checks that passed without changes

- Keys never appear in errors, CLI output, cache metadata, the built site, ledger entries or
  committed files (tests scan for common key formats).
- Keys are refused over plain HTTP to non-localhost hosts; TLS verification cannot be disabled.
- Prompt templates are filled in a single pass, so `{{...}}` inside news text is never
  expanded.
- The wrapper cannot be closed early (`</untrusted_source>`, case and spacing variants,
  zero-width characters, full-width brackets).
- A model that obeys injected text cannot push a forecast beyond 0.95, and thin evidence pulls
  it back toward the base rate.
- Lookahead: forecasts fail with `LookaheadError` if any news item, bar or source in the context
  is not strictly before T; stored future items never reach a prompt.
- All regexes run in well under 2 s on 100 KB pathological inputs.
- Workflows: every action pinned to a 40-character SHA, `permissions: {}` at the top, no
  `github.event` interpolation, data jobs serialised by a concurrency group.

## Recommendations for the maintainer

1. Protect `main` (no force pushes, no deletions). The hash chain detects edits, but a
   force-push could rewrite the git history that backs the commit links.
2. Enable GitHub's private vulnerability reporting and secret scanning.
3. Re-run `uvx pip-audit` and `uvx zizmor .github/workflows` when Dependabot opens PRs.
