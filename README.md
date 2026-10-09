# Trenches feed: crypto newscast for radio

Public endpoints (GitHub Pages):
- `https://water-bear86.github.io/trenches-feed/latest.json`: all four formats + `variables`
- `https://water-bear86.github.io/trenches-feed/latest-12min.json` | `latest-7min.json` | `latest-5min.json` | `latest-2.5min.json`
- `https://water-bear86.github.io/trenches-feed/schedule.json`: clock slot (Pacific) to format
- `https://water-bear86.github.io/trenches-feed/feed.xml`: RSS 2.0

Station usage: at each slot, look up the format in `schedule.json`, GET `latest-<fmt>.json`, substitute the template
variables, and read `full_script` (or `segments[].stories[]`).

## Template variables (left unfilled; listed in the `variables` object of every JSON)
Station/clock: `{{STATION_NAME}}`, `{{ANCHOR_NAME}}`, `{{TIME_OF_DAY_GREETING}}`, `{{TIME_SPOKEN}}`. Market: see "Prices and market figures" below.
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

## Prices and market figures: filled by the station
All live market figures are UNFILLED template variables in every published output (md, latest*.json, feed.xml), in the same
`{{DOUBLE_BRACE}}` style as `{{STATION_NAME}}`. The station substitutes them at playback from its own data (e.g. Pyth feeds via Helius RPC).
Full definitions (description, format, example, source hint, spoken-length estimate) are in the top-level `variables` object of
`latest.json` and every `latest-<fmt>.json`.

| Variable | Format | Example |
|---|---|---|
| `{{PRICES_AS_OF_SPOKEN}}` | spoken words | six twenty-nine Pacific time |
| `{{MARKET_DIRECTION}}` | fall / rise / are mixed / hold steady | fall |
| `{{MARKET_DIRECTION_PAST}}` | fell / rose / were mixed / held steady | fell |
| `{{BTC_PRICE}}` `{{ETH_PRICE}}` `{{SOL_PRICE}}` `{{HYPE_PRICE}}` | spoken words (BTC to nearest 100, ETH to 5, others whole dollars; script says "dollars") | eighty-one thousand seven hundred |
| `{{BTC_CHANGE_SPOKEN}}` (and ETH/SOL/HYPE) | spoken phrase incl. direction | down about two per cent / up about one and a half per cent / little changed |
| `{{BTC_CHANGE_PCT}}` (and ETH/SOL/HYPE) | digits, signed, 1 decimal (machine field, not read aloud) | -2.0 |
| `{{BTC_PRICE_DIGITS}}` | digits for written headline | $81,700 |
| `{{TOTAL_MARKET_CAP}}` | spoken words (script says "dollars") | two point seven eight trillion |
| `{{TOTAL_MARKET_CAP_CHANGE_SPOKEN}}` | spoken phrase incl. direction | down about five per cent |

Story-specific figures from research (e.g. a token's reported gain, a trader's liquidation level, ETF flow totals) stay as attributed text and refresh with each hourly research edition.

- `render_feed.py` and `publish.sh` never fill prices (env pre-fill only applies to STATION_NAME, ANCHOR_NAME, TIME_OF_DAY_GREETING, TIME_SPOKEN).
- `update_prices.py` is an optional local helper: it prints example values for every market variable from public APIs (CoinGecko, falling back to Coinbase, then Kraken) to test the station's substitution. It never touches published files.
- The `price-update` workflow is disabled and has no schedule. `pages.yml` deploys Pages on every push.
- Hourly research runs at :55 PT (separate job) and publishes with `publish.sh` (which `git pull --rebase`s first).
