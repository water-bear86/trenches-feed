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

UA = {"User-Agent": "trenches-feed/1.0", "Accept": "application/json"}
SOURCES = {
    "prices": "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,binancecoin&vs_currencies=usd&include_24hr_change=true&include_market_cap=true",
    "global": "https://api.coingecko.com/api/v3/global",
    "cg_trending": "https://api.coingecko.com/api/v3/search/trending",
    "gt_solana": "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1",
    "gt_base": "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1",
    "gt_bsc": "https://api.geckoterminal.com/api/v2/networks/bsc/trending_pools?page=1",
    "ds_boosts_top": "https://api.dexscreener.com/token-boosts/top/v1",
    "ds_boosts_latest": "https://api.dexscreener.com/token-boosts/latest/v1",
    "ds_profiles": "https://api.dexscreener.com/token-profiles/latest/v1",
    "fng": "https://api.alternative.me/fng/",
    "llama_sol_dex": "https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true",
}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
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
            if isinstance(data, dict) and (data.get("error") or data.get("status", {}).get("error_code")):
                raise ValueError("Provider returned an error object")
            record.update(status="ok", data=data)
        except (OSError, ValueError, TypeError, AttributeError):
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
        for c, v in data["prices"].items():
            L.append(f"- {c}: ${v['usd']:,} ({v['usd_24h_change']:+.2f}% 24h)")
    if "global" in data:
        g = data["global"]["data"]
        L.append(f"- Total mcap {money(g['total_market_cap']['usd'])} ({g['market_cap_change_percentage_24h_usd']:+.2f}% 24h), BTC dominance {g['market_cap_percentage']['btc']:.1f}%")
    if "fng" in data:
        f = data["fng"]["data"][0]; L.append(f"- Fear & Greed: {f['value']} ({f['value_classification']})")
    if "llama_sol_dex" in data:
        d = data["llama_sol_dex"]; L.append(f"- Solana DEX volume 24h: {money(d.get('total24h'))} ({d.get('change_1d')}% 1d)")
    L.append("")
    if "cg_trending" in data:
        L.append("## CoinGecko trending searches")
        for c in data["cg_trending"]["coins"]:
            i = c["item"]; ch = (i.get("data") or {}).get("price_change_percentage_24h", {}).get("usd")
            L.append(f"- {i['symbol']} ({i['name']}) 24h: {ch:+.1f}%" if ch is not None else f"- {i['symbol']} ({i['name']})")
        L.append("")
    for net in ("solana", "base", "bsc"):
        k = f"gt_{net}"
        if k not in data: continue
        L.append(f"## GeckoTerminal trending pools - {net}")
        for p in data[k]["data"][:15]:
            a = p["attributes"]; dex = p["relationships"]["dex"]["data"]["id"]
            L.append(f"- {a['name']} | FDV {money(a.get('fdv_usd'))} | vol24 {money(a['volume_usd']['h24'])} | 24h {a['price_change_percentage']['h24']}% | created {a['pool_created_at']} | {dex} | https://www.geckoterminal.com/{net}/pools/{a['address']}")
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
