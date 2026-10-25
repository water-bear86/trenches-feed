#!/usr/bin/env python3
"""Trenches feed - data gathering (re-runnable, no API keys needed).

Pulls free public APIs and writes:
  raw/<YYYY-MM-DD>/*.json      raw API responses
  data-<YYYY-MM-DD>.md         human-readable digest for the writer/host

Research and story writing are handled by news_engine.py / research.py. This collector does not:
  - Story selection & narrative: run X `search_news` (max_age_hours=48) for queries like
    "crypto bitcoin market", "memecoin pump.fun solana", "crypto ETF SEC regulation hack exploit",
    "BNB Chain Base memecoin four.meme", "memecoin narrative meta AI agents tokenized stocks",
    then X `search_posts_all` on any trending ticker to learn WHY it is running.
  - Filtering shill/bot spam (copy-paste "vote for $TICKER" campaigns), paid DexScreener boosts,
    and rugs vs. real runners.
  - Writing the spoken script + JSON (feed-<date>.md / feed-<date>.json).
Usage: python3 build_feed.py
"""
import json, os, sys, time, datetime, urllib.request
from pathlib import Path
from update_prices import cmc_assets, cmc_global, NoRedirect, get as market_get

UA = {"User-Agent": "trenches-feed/1.0", "Accept": "application/json"}
SOURCES = {
    "prices": "https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest?id=1,1027,5426,1839&convert=USD",
    "global": "https://pro-api.coinmarketcap.com/public-api/v1/global-metrics/quotes/latest",
    "ds_boosts_top": "https://api.dexscreener.com/token-boosts/top/v1",
    "ds_boosts_latest": "https://api.dexscreener.com/token-boosts/latest/v1",
    "ds_profiles": "https://api.dexscreener.com/token-profiles/latest/v1",
    "fng": "https://api.alternative.me/fng/",
    "llama_sol_dex": "https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true",
}

def get(url):
    if url.startswith("https://pro-api.coinmarketcap.com/"):
        return market_get(url)
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname not in ("api.dexscreener.com", "api.alternative.me", "api.llama.fi"):
        raise ValueError("Unsupported market source")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
        raw = r.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("API response exceeds the size limit")
    return json.loads(raw)


def collect(output_dir, names=None, fetch=get, pause=time.sleep):
    """Return this run's observations only; failed sources never reuse yesterday's data."""
    names = list(SOURCES) if names is None else list(names)
    if not names or any(name not in SOURCES for name in names):
        raise ValueError("Choose at least one known market source.")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sources = {}
    for name in names:
        observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        record = {"source_url": SOURCES[name], "observed_at": observed_at}
        try:
            data = fetch(SOURCES[name])
            if not isinstance(data, (dict, list)) or not data:
                raise ValueError("Empty or malformed market response")
            if isinstance(data, dict) and (data.get("error") or data.get("status", {}).get("error_code") not in (None, 0, "0")):
                raise ValueError("Provider returned an error object")
            if name == "prices" and len(cmc_assets(data, {1: "BTC", 1027: "ETH", 5426: "SOL", 1839: "BNB"})) != 4:
                raise ValueError("Incomplete or invalid CMC prices")
            if name == "global":
                cmc_global(data)
            record.update(status="ok", data=data)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            record.update(status="error", error="Market source failed or returned invalid data")
        sources[name] = record
        (output_dir / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        pause(0.4)
    snapshot = {"collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "sources": sources}
    (output_dir / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    return snapshot

def money(x):
    try: x = float(x)
    except (TypeError, ValueError): return "n/a"
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div: return f"${x/div:.2f}{s}"
    return f"${x:.2f}"

def main():
    base = os.path.dirname(os.path.abspath(__file__))
    day = datetime.date.today().isoformat()
    outdir = os.path.join(base, "raw", day)
    snapshot = collect(Path(outdir) / datetime.datetime.now().strftime("%H%M%S"))
    data = {k: value["data"] for k, value in snapshot["sources"].items() if value["status"] == "ok"}
    status = {k: value["status"] for k, value in snapshot["sources"].items()}

    L = [f"# Trenches data digest - {datetime.datetime.now().astimezone():%Y-%m-%d %H:%M %Z}", ""]
    L.append("## Source status"); L += [f"- {k}: {v}" for k, v in status.items()]; L.append("")
    L.append("## Majors / market")
    if "prices" in data:
        for symbol, value in cmc_assets(data["prices"], {1: "BTC", 1027: "ETH", 5426: "SOL", 1839: "BNB"}).items():
            L.append(f"- {symbol}: ${value['usd']:,} ({value['change_24h_pct']:+.2f}% 24h)")
    if "global" in data:
        try:
            g = cmc_global(data["global"])
            L.append(f"- Total mcap {money(g['total_market_cap'])} ({g['total_market_cap_yesterday_percentage_change']:+.2f}% 24h), BTC dominance {data['global']['data']['btc_dominance']:.1f}%")
        except (KeyError, TypeError, ValueError):
            L.append("- Global market metrics unavailable")
    if "fng" in data:
        f = data["fng"]["data"][0]; L.append(f"- Fear & Greed: {f['value']} ({f['value_classification']})")
    if "llama_sol_dex" in data:
        d = data["llama_sol_dex"]; L.append(f"- Solana DEX volume 24h: {money(d.get('total24h'))} ({d.get('change_1d')}% 1d)")
    L.append("")
    if "ds_boosts_top" in data:
        L.append("## DexScreener top boosted tokens (PAID promotion - not organic signal)")
        for x in data["ds_boosts_top"][:20]:
            L.append(f"- [{x.get('chainId')}] {x.get('tokenAddress')} boost={x.get('totalAmount')} {(x.get('description') or '')[:80].replace(chr(10), ' ')}")
        L.append("")
    out = os.path.join(base, f"data-{day}.md")
    with open(out, "w") as fh: fh.write("\n".join(L))
    print(f"wrote {out} and {outdir}/")
    for k, v in status.items(): print(k, v)
    return 0 if all(v == "ok" for v in status.values()) else 1

if __name__ == "__main__":
    sys.exit(main())
