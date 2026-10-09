#!/usr/bin/env bash
# Re-render with the public base URL, commit and push to GitHub Pages (main branch, repo root).
# Requires: gh/git already authenticated on this machine. No secrets are stored in the repo.
set -euo pipefail
cd "$(dirname "$0")"
export FEED_BASE_URL="${FEED_BASE_URL:-https://water-bear86.github.io/trenches-feed}"
python3 render_feed.py "$@"
git add -A
if git diff --cached --quiet; then echo "nothing to publish"; exit 0; fi
git commit -q -m "Feed update $(date '+%Y-%m-%d %H:%M %Z')"
git push -q origin main
echo "pushed; Pages usually updates within ~1 minute: $FEED_BASE_URL/latest.json"
