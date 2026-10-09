#!/usr/bin/env bash
# 1) pull API data (automatic)  2) [manual/agent] write editions/<date>.stories.json using X + web search
# 3) render md/json/latest.json/feed.xml
set -euo pipefail
cd "$(dirname "$0")"
python3 build_feed.py || echo "WARN: some API sources failed (see data-$(date +%F).md)"
if [ -f "editions/$(date +%F).pool.json" ]; then python3 render_feed.py "$(date +%F)"
else echo "No editions/$(date +%F).pool.json yet - write today's story pool (judgment step; see editions/_make_pool_2026-10-08.py), then run: python3 render_feed.py"; fi
