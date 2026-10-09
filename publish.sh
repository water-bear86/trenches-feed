#!/usr/bin/env bash
# Re-render with the public base URL, commit and push (pages.yml then deploys GitHub Pages).
# Pulls first (rebase) so it never conflicts with the price-update Action's commits.
# Requires: gh/git already authenticated on this machine. No secrets are stored in the repo.
set -euo pipefail
cd "$(dirname "$0")"
export FEED_BASE_URL="${FEED_BASE_URL:-https://water-bear86.github.io/trenches-feed}"
git stash -q --include-untracked || true
git pull -q --rebase origin main
git stash pop -q 2>/dev/null || true
python3 render_feed.py "$@"
git add -A
if git diff --cached --quiet; then echo "nothing to publish"; exit 0; fi
git commit -q -m "Feed update $(date '+%Y-%m-%d %H:%M %Z')"
for i in 1 2 3; do
  git push -q origin main && break
  # main moved (price Action): rebase our edition on top, keeping our generated files, then re-render
  git pull -q --rebase -X theirs origin main && python3 render_feed.py "$@" >/dev/null && git add -A && { git diff --cached --quiet || git commit -q --amend --no-edit; }
  sleep 3
done
echo "pushed; Pages deploys via Actions in ~1 minute: $FEED_BASE_URL/latest.json"
