#!/usr/bin/env bash
# Explicit publisher: validate first, stage generated artifacts only, never stash or rebase.
set -euo pipefail
cd "$(dirname "$0")"
export FEED_BASE_URL="${FEED_BASE_URL:-https://water-bear86.github.io/trenches-feed}"
dry_run=false
render_args=()
while (($#)); do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --pool)
      if (($# < 2)); then echo "--pool requires a path" >&2; exit 1; fi
      render_args+=(--pool "$2"); shift 2 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) render_args+=("$1"); shift ;;
    *) echo "Usage: publish.sh [--dry-run] [--pool PATH | YYYY-MM-DD]" >&2; exit 1 ;;
  esac
done
if ! $dry_run; then
  if [[ "$(git branch --show-current)" != main ]]; then
    echo "Publishing requires the main branch." >&2; exit 1
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Commit or set aside tracked changes before publishing." >&2; exit 1
  fi
fi
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
python3 render_feed.py "${render_args[@]}" --output-dir "$staging"
if $dry_run; then
  echo "Dry run passed; repository files and Git state were not changed."
  exit 0
fi
artifacts=()
while IFS= read -r name; do
  artifacts+=("$name")
  cp "$staging/$name" "$name"
done < <(python3 - "$staging" <<'PY'
import pathlib, sys
for path in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    if (path.name in {"latest.json", "schedule.json", "feed.xml"}
            or path.name.startswith("latest-") and path.suffix == ".json"
            or path.name.startswith("feed-") and path.suffix in {".json", ".md"}):
        print(path.name)
PY
)
git add -- "${artifacts[@]}"
if git diff --cached --quiet; then echo "No generated changes to publish."; exit 0; fi
git commit -m "Feed update $(date -u '+%Y-%m-%d %H:%M UTC')"
# A rejected push is a failure. Resolve concurrent updates explicitly; do not rewrite research.
git push origin HEAD:main
echo "Pushed feed artifacts; GitHub Pages deployment is pending: $FEED_BASE_URL/latest.json"
