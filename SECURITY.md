# Security policy

## Supported versions

Proofcast is in early development. Only the latest commit on `main` receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private
vulnerability reporting instead: open the repository's **Security** tab and choose
**Report a vulnerability**.

Include what you found, how to reproduce it, and the impact you expect. We aim to acknowledge
reports within 7 days.

## What we care about most

- Leaking API keys or other secrets (logs, errors, the generated site, committed data).
- Prompt injection from news, filings or web text that changes forecasts or escapes the
  `<untrusted_source>` boundary.
- Anything that lets someone edit or delete ledger entries without `proofcast ledger verify`
  detecting it.
- Lookahead: data published after a forecast's timestamp reaching the agents for that forecast.
- Workflow issues: over-broad GitHub Actions permissions or untrusted input interpolated into
  shell commands.

Proofcast never executes trades and holds no funds; reports about "trading losses" are out of
scope.
