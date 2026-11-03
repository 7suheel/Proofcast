#!/usr/bin/env bash
# Commit changed data/ledger files with a clear message and push to main.
# The git history is the audit trail, so every scheduled step commits separately.
set -euo pipefail
msg="$1"
git config user.name "proofcast-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -- data ledger
if git diff --cached --quiet; then
  echo "nothing to commit for: $msg"
  exit 0
fi
git commit -q -m "$msg"
for attempt in 1 2 3; do
  if git pull -q --rebase origin main && git push -q origin HEAD:main; then
    echo "pushed: $msg"
    exit 0
  fi
  echo "push attempt $attempt failed, retrying"
  sleep $((attempt * 5))
done
exit 1
