# Trenches feed: crypto newscast for radio

Public endpoints (GitHub Pages):
- `https://water-bear86.github.io/trenches-feed/latest.json`: all four formats + `variables`
- `https://water-bear86.github.io/trenches-feed/latest-12min.json` | `latest-7min.json` | `latest-5min.json` | `latest-2.5min.json`
- `https://water-bear86.github.io/trenches-feed/schedule.json`: clock slot (Pacific) to format
- `https://water-bear86.github.io/trenches-feed/feed.xml`: RSS 2.0

Station usage: at each slot, look up the format in `schedule.json`, GET `latest-<fmt>.json`, substitute the template
variables, and read `full_script` (or `segments[].stories[]`).

## Template variables (left unfilled; listed in the `variables` object of every JSON)
`{{STATION_NAME}}`, `{{ANCHOR_NAME}}`, `{{TIME_OF_DAY_GREETING}}`, `{{TIME_SPOKEN}}`.
Optional pre-fill at render: `STATION_NAME="Example FM" ANCHOR_NAME="Alex" python3 render_feed.py`. Word counts include an estimate for each variable.

## Style
All copy follows `STYLE_GUIDE.md`: a public-broadcaster hourly newscast (neutral, attributed, Canadian spelling, jargon explained).

## Pipeline
1. `build_feed.py` (automatic): CoinGecko, GeckoTerminal trending pools (solana/base/bsc), DexScreener, Fear & Greed, DefiLlama to `raw/<date>/`, `data-<date>.md` (raw/ is not published).
2. Judgment step (not scripted): X `search_news` (max_age_hours=48) + `search_posts_all` + web search; verify, drop shill/bot spam and paid boosts;
   write `editions/_make_pool_<date>.py` in the STYLE_GUIDE voice, producing `editions/<date>.pool.json` (priority 1-3, text_medium/extra/short/flash, source_urls, timestamp).
3. `render_feed.py`: fits the four formats (12/7/5/2.5 min at 150 wpm) and writes `latest*.json`, `schedule.json`, `feed.xml`, `feed-<date>*.md|json`.
4. `publish.sh`: re-renders with `FEED_BASE_URL`, commits and pushes to GitHub Pages.

Not financial advice. Memecoin data is unverified.
