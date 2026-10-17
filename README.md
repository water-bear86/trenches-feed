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

## News engine

Python 3.10+ on macOS or Linux; no Python packages are required. The engine extends the existing collectors
and renderer with persistent story identity, revisions, source-attributed research and inspectable off-air outputs.
It does not connect to the radio, synthesize speech, restart station services or publish automatically.

## Live off-air radar and newsroom

```bash
# Continuous collection, local newsroom and bounded research worker.
python3 radar.py run
# Open http://127.0.0.1:8791 in your browser.

# Continuous market collection and operator review, with no model requests.
python3 radar.py run --no-research

# One public-market collection, or inspect the durable state.
python3 radar.py once
python3 radar.py status
```

The existing Python/SQLite engine remains the source of stories and editions. The radar adds market polling,
optional stream readers, deterministic signals, durable candidates and research jobs to the **same state ledger**.
Use the same `--state-dir` for the radar and manual engine runs to share the quota; isolated acceptance ledgers
do not coordinate usage. Starting a second collector against the same state directory is rejected. The browser
server binds only to `127.0.0.1`, restricts host/origin headers, and requires a per-process token for queue/dismiss
actions. It serves only its named UI assets and API. It has no publish or broadcast action.

`radar-config.json` controls the watchlists, thresholds, cadence and lane allocations:

- Discover Solana token profiles and GeckoTerminal trending-pool token addresses every two minutes. Prefer
  trending discoveries in a bounded watchlist of 60 tokens; `watch_tokens` pins explicit contracts. This samples
  discoverable tokens, not every Solana launch or transaction. Discovery URLs, profiles and boosts are not proof
  of organic demand, wallet ownership or token safety.
- Fetch batched DEX Screener pool values every 30 seconds, backing off on failure. Only successful fetches
  advance the observations. Upstream update timestamps are unavailable: the UI explicitly labels **fetch** time.
- Compare five-minute volume with `(hourly volume - five-minute volume) / 11`, the average five-minute pace
  over the preceding 55 minutes. Also surface significant five-minute price moves, newly active pools, and large
  USD liquidity changes between comparable recent local fetches. USD liquidity can change with token prices;
  it does not establish liquidity removal or a rug. Paid boosts are exposed as context and never scored alone.
- Candidate evidence becomes stale after two minutes without a fresh matching signal. A candidate has a separate
  ten-minute maximum lifetime that repeated identical observations do not renew. Rearming requires the 30-minute
  subject cooldown. Price/liquidity direction reversals create distinct candidates while retaining the shared
  token research cooldown. Market observations are retained for six hours; stream excerpts for seven days. Story history,
  job history and quota reservations remain inspectable. No claim of complete historical chain coverage is made.
  The All candidates view retains expired and dismissed evidence with explicit status; Needs research shows fresh work.
- A single research worker investigates one candidate at a time while collection continues. It re-fetches an
  individual pool before reserving a model call, so different pools cannot share a batched URL as their event
  identity. That cheap cross-check backs off on failure without spending a model request. Targeted research includes
  the exact contract, observed values, source and prior story state; unrelated returned stories are excluded.
- Allocate **12 background calls and 12 triggered calls per UTC day**, under the existing global 24-request cap.
  Background sweeps run in two-hour UTC slots; missed slots are skipped. Automatic triggered calls require a score
  of at least 75 and normally have a 15-minute minimum spacing. Urgent signals scoring 90 or higher can bypass
  that spacing and move ahead of queued lower-priority work; the daily cap, single worker and 30-minute
  token/subject cooldown still apply. These are editorial heuristics, not calibrated probabilities or trusted
  radio breaking flags. Failed, blocked,
  incomplete and potentially billed requests are not retried automatically. Manual runs also consume the global
  cap. This is a request budget, not a currency cap; provider/tool costs can differ between jobs.
- Queue or dismiss candidates in the UI. Search and status filters inspect the live candidate queue; the
  Investigations view retains completed, failed, expired and uncertain jobs. A queue action with research paused
  persists the job but makes no model request. Expired queued work is withheld when the worker resumes.
- Radar-generated market copy expires five minutes after its observation; attributed announcements retain their
  original event deadlines. All copy remains an **off-air, source-attributed draft**. The station must check its
  expiry and evidence before any future integration; no speech synthesis or on-air acceptance is provided here.

The UI refreshes every five seconds and reports collector heartbeat separately from the last actual scan. A
successful old fetch becomes stale rather than remaining "Connected" indefinitely. The collector must be running
for these cadences; stopping the terminal stops collection. This command does not install an OS service or a Codex
automation. Source-event-to-detection latency cannot be measured without an upstream update timestamp. Successful
targeted jobs record request duration and detection-to-result duration in `radar_meta.last_latency`.
Exact normalized research inputs, individual-pool cross-checks, provider wrappers and validated envelopes are saved
privately in `.state/research/`. These audit captures and the SQLite runtime are excluded from source deliveries.

### Optional X and Solana streams

Market polling, the UI and the Grok CLI require no additional Python packages. Optional Solana WebSocket
collection uses `python3 -m pip install -r requirements.txt` in your chosen virtual environment.

- **X:** Set `RADAR_ENABLE_X=1` and `X_BEARER_TOKEN` in the process environment, then restart the radar. A Grok
  browser login is not X developer API access. `x_handles` defines the account allowlist. The adapter adds only
  missing account rules, never deletes another consumer's rules, validates incoming author identities, follows
  post edits and deduplicates post IDs. Use a dedicated app/stream connection; upstream access/pricing is separate.
  A 500-received-record daily cap includes duplicates and errors and persists across restarts. Posts remain claims
  requiring research, not verified announcements or trusted breaking flags. Stream disconnects expose coverage gaps.
- **Solana:** Set `RADAR_ENABLE_SOLANA=1` and a secure `SOLANA_RPC_WS_URL`, and add public addresses to
  `solana_mentions`. Credentials belong in the environment, never tracked JSON. One confirmed `logsSubscribe`
  subscription is used per address. Failed transactions are excluded. A mention is not proof of wallet ownership,
  token sale, graduation or exploit, so raw transaction candidates are below the automatic investigation threshold.
  The daily cap is 2,000 stored events. Reconnects resubscribe but do not backfill missed history; gaps are disclosed.

Neither optional source is enabled merely by finding a credential in the environment. Keep provider URLs and
tokens out of exported state, screenshots and Git. This project does not create accounts, buy subscriptions,
extract browser credentials or reuse Grok authentication for another provider.

Primary protocol references: [DEX Screener API](https://docs.dexscreener.com/api/reference),
[X filtered stream](https://docs.x.com/x-api/posts/filtered-stream/introduction),
[Solana logsSubscribe](https://solana.com/docs/rpc/websocket/logssubscribe), and
[websocket-client](https://websocket-client.readthedocs.io/en/latest/core.html).

## Batch engine

1. Collect current CoinGecko, GeckoTerminal, DexScreener, Fear & Greed and DefiLlama observations.
   Each run gets its own snapshot directory and per-source success/failure records; failed sources never reuse old data.
2. Research original announcements/reporting and specific X posts through either the signed-in Grok CLI or the xAI
   Responses API. Both paths request structured stories, four copy lengths and claim-specific source URLs.
3. Validate timestamps, citations, claim references, URLs and copy. Keep unverified leads out of bulletins.
   Store stories, aliases, factual revisions, research runs and usage reservations in SQLite.
4. Preserve current stories in order, append ordinary developments, and prepend only locally configured breaking IDs.
   A changed headline or rewritten copy does not make the same sourced facts new. Corrections create revisions;
   withdrawals remove stories and invalidate a bulletin when no eligible stories remain.
5. Render the existing four JSON formats, bundled JSON, Markdown, schedule and RSS into an off-air output directory.
   Output preparation completes before file replacement; each file is replaced atomically and `latest.json` is written last.

### Run off-air

```bash
# Public data only; does not call a model.
python3 news_engine.py collect

# Research and render using the installed, signed-in Grok CLI.
python3 news_engine.py run

# Inspect persisted state and the UTC daily research count.
python3 news_engine.py status

# Import an explicitly attributed operator research envelope, without a model call.
python3 news_engine.py run --research-file /absolute/path/research.json

# Separate state/output locations are useful for acceptance checks.
python3 news_engine.py run --state-dir /private/path/state --output-dir /private/path/preview
```

Default state is `.state/`, including immutable research inputs, per-run market snapshots, revision history and
`editions/<research_id>.pool.json`. Completed raw provider responses are captured privately under `provider/`,
including incomplete/cancelled results when available, so diagnostics do not require another paid request.
The default feed output is `.state/preview/`. These paths, dotenv files and
SQLite files are ignored by Git. Keep external state/output locations private too. Never put credentials in story pools.

`engine-config.json` selects `backend: "grok-cli"` by default. Install and sign into `grok` normally first; the engine
lets the client manage its login and never extracts its browser tokens. Headless research exposes only `web_search`
and `web_fetch`; shell, file access, editing and subagents are unavailable. The client must support `--json-schema`,
`--prompt-file`, `--no-subagents`, `--tools` and `--max-turns`. Its configured model is used unless `XAI_MODEL` is set.
The CLI adapter validates the documented JSON completion wrapper and fails closed on incomplete output.
Each hourly batch asks for at most four developments; up to eight active stories can appear in the rolling pool.
CLI `reasoning_effort` defaults to `low` for bounded news briefs, with at most six turns and a four-minute deadline.

For a server with API credentials, select `backend: "xai"`, export `XAI_API_KEY` and set `XAI_MODEL` to a currently
supported model with search and structured output. See `.env.example`; the engine does not load dotenv files itself.
The REST adapter uses `https://api.x.ai/v1/responses`, `x_search`, `web_search` and `text.format` JSON Schema.
Credential-bearing HTTP redirects are rejected. Provider failures and uncertain timeouts are not automatically retried.

For standalone batch use, hourly research fits the quota. `daily_research_calls` is **24 requests per UTC day**, including failed or
uncertain attempts reserved before execution. This is a request quota, not a dollar spending cap: provider search/tool
usage can vary within one job. Both backends have a turn limit and a process/request deadline; `max_output_tokens`
applies to the REST backend. Actual provider usage is saved when the provider exposes it.
The continuous radar has the internal cadence described above. For standalone batches, invoke `run` from the eventual host's existing
scheduler after validating access and the radio adapter. An exclusive process lock prevents overlapping engine runs against
the same state directory.

### Freshness and editorial boundaries

- `generated_at` and `researched_at` describe research, never rerendering. `rendered_at` describes rendering.
  `expires_at` is an explicit consumer deadline. A consumer must check it immediately before speech; a static
  `available: true` is not a substitute for checking the clock. Same-facts research does not renew a story or edition.
  Story event deadlines use the configured lookback window (48 hours by default); the edition has a separate
  one-hour validity window. Still-current announcements can carry into an edition containing new developments,
  retaining their original research/event dates and revisions.
- Every story exports a stable `story_id` and factual `revision` alongside the legacy format-specific `id`.
  Claims and source URLs support inspection and future audio invalidation; no audio cache is created here.
- `reuse` reports the target and actual share in **estimated spoken words**, not measured audio. Short bulletins
  can use different variants; actual speech timing and the station's roughly 80% audio reuse remain station concerns.
- Format names describe maximum word budgets at an assumed 150 wpm. `underfilled` is explicit; scarce news does
  not produce invented filler. Renderers remove whole stories or choose shorter variants, never cut spoken sentences.
- `coverage_terms` is an explicit, conservative keyword guard on **claims**, not headlines. It excludes unrelated
  stock-market/macro filler even when the headline adds a crypto angle. New project terminology may require an
  editorial configuration update. This guard is not semantic verification. Out-of-window and out-of-scope leads
  are recorded under `rejected`; eligible current stories can still render from the same batch.
- Recognised market API observations get a one-hour story deadline, shorter than announcement/report deadlines.
  Collector URLs are recorded as encountered sources independently of the model's search citations.
- `breaking_story_ids` is a local editorial setting. Research output cannot set breaking flags or control the clock.
- Citation matching is provenance validation, **not an independent truth check**. API citations show encountered
  URLs; CLI citations are model-reported. All output remains `source_attributed`, not automatically verified fact.
  Sensitive allegations need editorial/source review before broadcast. The engine has no automatic publish path.
- `last_selected_at` means selected for an off-air edition, not played. Keep radio airing history separate.

Operator research imports use `schema_version: 1`, a unique `research_id`, timezone-aware `researched_at`,
`provenance: "operator"`, `citations: ["https://..."]` and `stories: [...]`. Story fields are defined in
`research.STORY_SCHEMA`: `event_key`, specific `event_url`, `segment` (`trenches`, `broader`, `metas`),
`priority` (1–3), `headline`, `occurred_at`, `status` (`reported`, `unverified`, `withdrawn`),
`correction_of` (known story ID or empty string), `claims` (each with `fact` and `source_urls`),
and `text_medium`, `text_extra`, `text_short`, `text_flash`. URLs in claims and the event URL must occur in citations.
`research_id` is idempotent; reusing it for different input is rejected.

### Render and publish explicitly

```bash
python3 render_feed.py --pool .state/editions/RESEARCH_ID.pool.json --output-dir .state/review
bash publish.sh --dry-run --pool .state/editions/RESEARCH_ID.pool.json

# Historical review only: preserves old research dates and sets available=false.
python3 render_feed.py 2026-10-08 --allow-stale --output-dir .state/historical
```

Legacy `editions/<date>.pool.json` files remain readable. Pools without explicit expiry get a conservative
one-hour research validity window; old pools cannot be made current by rerendering.
The old `build_feed.sh` remains a data/manual-pool helper; `news_engine.py run` is the automated pipeline.

After reviewing the edition and committing implementation changes on `main`, `bash publish.sh --pool PATH`
renders into temporary staging, copies and stages **only generated feed artifacts**, commits and pushes to `main`.
It refuses staged/tracked local changes, never stashes or rebases other work, and reports rejected pushes as failures.
GitHub Pages deployment is asynchronous; a successful push does not prove delivery or broadcast acceptance.

### Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile news_engine.py research.py build_feed.py render_feed.py
bash -n publish.sh
```

Tests isolate SQLite, files, quotas and model transports. They exercise retitled stories, idempotency, corrections,
withdrawals, stale inputs, source failures, legacy rendering, all format word budgets and publishing dry runs.
They do not establish live model quality, independent factual verification, synthesized timing or on-air integration.

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
- The historical hourly research job was external to this repository. The new engine does not install or alter it.

## Donate

If you found this to be useful, consider donating by sending magic internet monies to:

```text
sol: 79TNuyFNZWhDeFF1RUNA5Xk9Pccvb7xPYqLukBxCeWbb
evm: 0xa2c0abd1a1fcb5aee12f80651ae7f646371a66ed
```
