# Contributing to Proofcast

Thanks for helping! Please read [CLAUDE.md](CLAUDE.md) first: its hard rules apply to every
contribution, human or AI-written.

## Setup

```
uv sync
uv run proofcast --version
```

## Before you open a pull request

Run all checks locally; CI runs the same ones:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv lock --check
```

## Rules of thumb

- **Every change ships with tests.** Tests never touch the network or a real LLM: use recorded
  fixtures in `tests/fixtures`, `httpx.MockTransport` and `FakeProvider`.
- **No lookahead.** Any new data must carry the time it became public.
- **No hype.** Do not add wording such as "buy", "sell", "safe" or accuracy claims that the track
  record does not prove.
- **Ledger entries are never edited.** Corrections are new entries.
- **Few dependencies.** Discuss new dependencies in an issue first.
- **Never commit secrets.** Keys come from environment variables.
- Keep pull requests small and focused, with a clear description.

## Good first issues

See [docs/first-issues.md](docs/first-issues.md) once it exists, or issues labelled
`good first issue`.
